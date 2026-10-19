"""Linux platform module (dragshift/platform_linux.py) and the dispatch points that use it.

The module only holds fallbacks so far; these tests pin the documented fallback values,
that the dispatch helper never imports the module on other platforms, and that every
dispatch point reaches the module on Linux (with fake bodies patched in)."""
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux dispatch")


@pytest.fixture
def lx():
    from dragshift import platform_linux
    return platform_linux


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- module
def test_import_needs_no_dbus_libraries():
    code = ("import sys, dragshift.platform_linux; "
            "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
            "('gi', 'dbus', 'jeepney', 'dbus_next', 'dbus_fast', 'PySide6')); "
            "assert not bad, bad")
    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)


def test_fallbacks(lx, tmp_path):
    assert lx.pointer_state() is None
    assert lx.extension_available() is False
    assert lx.capture(0, 0, 10, 10) is None
    assert lx.capture(0, 0, 10, 10, exclude_self=True) is None
    assert lx.wallpaper_crop(0, 0, 10, 10) is None
    assert lx.autostart_enabled() is False
    assert lx.set_autostart(True) is None
    assert lx.set_autostart(False) is None
    assert lx.reveal(tmp_path / "x.png") is False


def test_key_masks_are_clutter_modifiers(lx):
    assert lx.KEY_MASKS == {"shift": 1, "ctrl": 4, "alt": 8}
    assert lx.BUTTON1_MASK == 256


# ---------------------------------------------------------------- never imported elsewhere
NOT_LINUX = """
import sys
if sys.platform.startswith("linux"):
    sys.platform = "darwin"          # simulate macOS before anything is imported
from PySide6.QtWidgets import QApplication
qa = QApplication([])
from dragshift import glass, radial, settings, toasts, trigger
w = trigger.DragShiftWatcher(lambda: False, lambda: False)
w.start()
glass.exclude_from_capture(0)
glass.grab_screen(0, 0, 10, 10)
settings.autostart_enabled()
radial.RadialMenu(lambda p: {}, lambda p: "")
assert w._linux is None
assert "dragshift.platform_linux" not in sys.modules
print("OK")
"""


def test_module_never_imported_off_linux(tmp_path):
    """On Windows/macOS (simulated as macOS on a Linux host) no dispatch point imports it."""
    import os
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-c", NOT_LINUX], cwd=root, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stderr


# ---------------------------------------------------------------- trigger
@pytest.fixture
def watcher(qapp, monkeypatch):
    from dragshift import trigger

    class NoCursor:  # QCursor.pos() is stale under XWayland: the Linux path must not use it
        @staticmethod
        def pos():
            raise AssertionError("QCursor.pos() used on Linux")

    monkeypatch.setattr(trigger, "QCursor", NoCursor)
    w = trigger.DragShiftWatcher(lambda: False, lambda: False)
    yield w
    w.timer.stop()


@linux_only
def test_watcher_idle_without_pointer_data(watcher, lx):
    assert watcher._linux is lx
    watcher.start()
    assert not watcher.timer.isActive()
    shown = []
    watcher.show_menu.connect(shown.append)
    watcher._tick()  # pointer_state() is None: nothing happens
    assert shown == [] and watcher._down_at is None


@linux_only
def test_watcher_uses_pointer_state(watcher, lx, monkeypatch):
    monkeypatch.setattr(lx, "extension_available", lambda: True)
    watcher.start()
    assert watcher.timer.isActive()
    watcher.timer.stop()
    shown = []
    watcher.show_menu.connect(shown.append)
    seq = iter([(100, 100, True, 0), (102, 100, True, lx.SHIFT_MASK), (130, 100, True, lx.SHIFT_MASK)])
    monkeypatch.setattr(lx, "pointer_state", lambda: next(seq))
    for _ in range(3):
        watcher._tick()
    assert [(p.x(), p.y()) for p in shown] == [(130, 100)]


@linux_only
def test_watcher_trigger_key_maps_to_mask(watcher, lx, monkeypatch):
    watcher.set_key("ctrl")
    monkeypatch.setattr(lx, "pointer_state", lambda: (5, 5, True, lx.SHIFT_MASK))
    assert watcher._state() == (True, False, False)
    monkeypatch.setattr(lx, "pointer_state", lambda: (5, 5, False, lx.CONTROL_MASK | lx.SHIFT_MASK))
    assert watcher._state() == (False, True, False)


# ---------------------------------------------------------------- glass
@linux_only
def test_exclude_from_capture(lx, monkeypatch):
    from dragshift import glass
    assert glass.exclude_from_capture(0) is False
    monkeypatch.setattr(lx, "extension_available", lambda: True)
    assert glass.exclude_from_capture(0) is True


@linux_only
def test_grab_screen_uses_capture(qapp, lx, monkeypatch):
    from dragshift import glass
    calls = []
    img = Image.new("RGB", (30, 20), (1, 2, 3))
    monkeypatch.setattr(lx, "capture", lambda *a, **k: calls.append((a, k)) or img)
    assert glass.grab_screen(5, 6, 30, 20) is img
    assert glass.grab_screen(5, 6, 30, 20, below_window=1) is img
    assert calls == [((5, 6, 30, 20), {"exclude_self": False}), ((5, 6, 30, 20), {"exclude_self": True})]


@linux_only
def test_grab_screen_falls_back_to_qt(qapp, lx, monkeypatch):
    from dragshift import glass
    calls = []
    monkeypatch.setattr(lx, "capture", lambda *a, **k: calls.append(a))
    im = glass.grab_screen(0, 0, 40, 30)
    assert calls == [(0, 0, 40, 30)]
    assert im is None or im.size == (40, 30)


# ---------------------------------------------------------------- radial menu
@linux_only
def test_menu_backdrop_from_wallpaper(qapp, lx, monkeypatch):
    from PySide6.QtCore import QPoint, QThreadPool
    from dragshift import radial
    calls = []
    monkeypatch.setattr(lx, "capture", lambda *a, **k: calls.append(("capture", a)))
    wall = Image.new("RGB", (radial.SIZE, radial.SIZE), (10, 120, 200))
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: calls.append(("wallpaper", a)) or wall)
    menu = radial.RadialMenu(lambda p: {}, lambda p: "")
    try:
        menu.popup(QPoint(300, 300))
        tl = menu.pos()
        assert calls == [("capture", (tl.x(), tl.y(), radial.SIZE, radial.SIZE)),
                         ("wallpaper", (tl.x(), tl.y(), radial.SIZE, radial.SIZE))]
        assert menu._raw_backdrop is not None
        assert menu._raw_backdrop.getpixel((2, 2))[:3] == (10, 120, 200)
        assert menu.backdrop is not None
    finally:
        QThreadPool.globalInstance().waitForDone(10000)
        menu.hide()


# ---------------------------------------------------------------- toasts / settings
@linux_only
def test_reveal_dispatch(lx, monkeypatch, tmp_path):
    from dragshift import toasts
    spawned, asked = [], []
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    f = tmp_path / "out.png"
    monkeypatch.setattr(lx, "reveal", lambda p: asked.append(p) or False)
    toasts.reveal(f)
    assert asked == [str(f)]
    assert spawned == [["xdg-open", str(tmp_path)]]   # fallback when the module can't
    spawned.clear()
    monkeypatch.setattr(lx, "reveal", lambda p: True)
    toasts.reveal(f)
    assert spawned == []


@linux_only
def test_autostart_dispatch(lx, monkeypatch):
    from dragshift import settings
    assert settings.autostart_enabled() is False
    seen = []
    monkeypatch.setattr(lx, "autostart_enabled", lambda: True)
    monkeypatch.setattr(lx, "set_autostart", seen.append)
    assert settings.autostart_enabled() is True
    settings.set_autostart(True)
    settings.set_autostart(False)
    assert seen == [True, False]
