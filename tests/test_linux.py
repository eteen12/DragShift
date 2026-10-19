"""Linux platform module (dragshift/platform_linux.py) and the dispatch points that use it.

The module only holds fallbacks so far. These tests check behaviour through the app's
public functions: what Linux does with the fallbacks, what it does once the module's bodies
answer (fakes patched in), that nothing imports the module off Linux, and the trigger state
machine on every platform."""
import gc
import subprocess
import sys
import time
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


@linux_only
def test_fallbacks_keep_todays_linux_behaviour(qapp, monkeypatch, tmp_path):
    from dragshift import glass, toasts
    assert glass.exclude_from_capture(0) is False
    assert glass.grab_screen(0, 0, 40, 30).size == (40, 30)          # Qt's grab, as before
    spawned = []
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    toasts.reveal(tmp_path / "out.png")
    assert spawned == [["xdg-open", str(tmp_path)]]


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
class FakeCursor:
    """Stands in for QCursor: the position the watcher reads, and how often it reads it."""
    at = (0, 0)
    reads = 0

    @classmethod
    def pos(cls):
        from PySide6.QtCore import QPoint
        cls.reads += 1
        return QPoint(*cls.at)


@pytest.fixture
def watcher(qapp, monkeypatch):
    from dragshift import trigger
    FakeCursor.at, FakeCursor.reads = (0, 0), 0
    monkeypatch.setattr(trigger, "QCursor", FakeCursor)
    menu = {"visible": False, "got_drag": False}
    w = trigger.DragShiftWatcher(lambda: menu["visible"], lambda: menu["got_drag"])
    w.menu = menu
    w.events = []
    w.show_menu.connect(lambda p: w.events.append(("show", p.x(), p.y())))
    w.toggle_ring.connect(lambda: w.events.append(("toggle",)))
    w.hide_menu.connect(lambda: w.events.append(("hide",)))
    yield w
    w.timer.stop()
    if LINUX:
        w._probe.stop()


def test_tick_state_machine(watcher, monkeypatch):
    """Every platform: press, move past the threshold with the key held, re-tap the key,
    release. The state source is faked, so this runs on the Windows/macOS CI jobs too."""
    from PySide6.QtTest import QTest
    watcher._linux = None                      # read the position from QCursor everywhere
    state = {"s": (False, False, False)}
    monkeypatch.setattr(watcher, "_state", lambda: state["s"])

    def tick(at, lbtn, key):
        FakeCursor.at, state["s"] = at, (lbtn, key, False)
        watcher._tick()

    tick((100, 100), True, False)              # press
    tick((105, 100), True, True)               # key down, but only 5 px moved
    assert watcher.events == []
    tick((115, 100), True, True)               # past the 10 px threshold: menu
    tick((140, 100), True, True)               # still dragging: no second menu
    assert watcher.events == [("show", 115, 100)]
    watcher.menu.update(visible=True, got_drag=True)
    tick((140, 100), True, False)              # key up
    tick((140, 100), True, True)               # key tapped again: other ring
    assert watcher.events[-1] == ("toggle",)
    tick((140, 100), False, False)             # drop
    assert watcher.events[-1] == ("toggle",)   # hide_menu is delayed 120 ms for the drop
    deadline = time.monotonic() + 10           # wait for it, however slow the CI runner is
    while len(watcher.events) < 3 and time.monotonic() < deadline:
        QTest.qWait(10)
    assert watcher.events == [("show", 115, 100), ("toggle",), ("hide",)]


def test_drag_without_key_opens_nothing(watcher, monkeypatch):
    watcher._linux = None
    state = {"s": (True, False, False)}
    monkeypatch.setattr(watcher, "_state", lambda: state["s"])
    for x in range(100, 200, 10):
        FakeCursor.at = (x, 100)
        watcher._tick()
    assert watcher.events == []


@linux_only
def test_watcher_idle_without_pointer_data(watcher):
    watcher.start()
    assert not watcher.timer.isActive()        # no extension: no polling, only the probe
    assert watcher._probe.isActive()
    watcher._tick()
    assert watcher.events == [] and FakeCursor.reads == 0


@linux_only
def test_watcher_follows_pointer_state_not_qcursor(watcher, lx, monkeypatch):
    monkeypatch.setattr(lx, "extension_available", lambda: True)
    watcher.start()
    assert watcher.timer.isActive()
    watcher.timer.stop()
    FakeCursor.at = (900, 900)                 # stale XWayland cursor: must be ignored
    seq = iter([(100, 100, True, 0), (102, 100, True, lx.SHIFT_MASK), (130, 100, True, lx.SHIFT_MASK)])
    monkeypatch.setattr(lx, "pointer_state", lambda: next(seq))
    for _ in range(3):
        watcher._tick()
    assert watcher.events == [("show", 130, 100)] and FakeCursor.reads == 0


@linux_only
def test_watcher_trigger_key_maps_to_modifier(watcher, lx, monkeypatch):
    watcher.set_key("ctrl")
    seq = iter([(100, 100, True, lx.SHIFT_MASK), (130, 100, True, lx.SHIFT_MASK),
                (150, 100, True, lx.CONTROL_MASK)])
    monkeypatch.setattr(lx, "pointer_state", lambda: next(seq))
    for _ in range(3):
        watcher._tick()
    assert watcher.events == [("show", 150, 100)]


# ---------------------------------------------------------------- glass
@linux_only
def test_exclude_from_capture(lx, monkeypatch):
    from dragshift import glass
    assert glass.exclude_from_capture(0) is False
    monkeypatch.setattr(lx, "extension_available", lambda: True)
    assert glass.exclude_from_capture(0) is True


@linux_only
def test_grab_screen_capture_is_normalised(qapp, lx, monkeypatch):
    from dragshift import glass
    calls = []
    big = Image.new("RGB", (60, 40), (1, 2, 3))      # e.g. a capture at 2x device scale
    monkeypatch.setattr(lx, "capture", lambda *a, **k: calls.append(k["exclude_self"]) or big)
    im = glass.grab_screen(5, 6, 30, 20)
    assert im.size == (30, 20) and glass.LAST_SOURCE == "capture"
    glass.grab_screen(5, 6, 30, 20, below_window=1)
    assert calls == [False, True]      # non-zero below_window = leave our own windows out


@linux_only
def test_grab_screen_falls_back_to_wallpaper(qapp, lx, monkeypatch):
    from dragshift import glass
    wall = Image.new("RGB", (30, 20), (9, 9, 9))
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: wall)
    for below in (0, 1):
        assert glass.grab_screen(5, 6, 30, 20, below_window=below) is wall
        assert glass.LAST_SOURCE == "wallpaper"


@linux_only
def test_grab_screen_qt_only_when_self_needs_no_excluding(qapp, lx, monkeypatch):
    from dragshift import glass
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: None)   # e.g. not a GNOME desktop
    im = glass.grab_screen(0, 0, 40, 30)              # fallbacks: no capture, no wallpaper
    assert im is not None and im.size == (40, 30) and glass.LAST_SOURCE == ""
    assert glass.grab_screen(0, 0, 40, 30, below_window=1) is None


# ---------------------------------------------------------------- radial menu
def _popup(radial, at):
    from PySide6.QtCore import QPoint, QThreadPool
    menu = radial.RadialMenu(lambda p: {}, lambda p: "")
    menu.popup(QPoint(*at))
    gc.collect()      # garbage goes here, not in the render thread's GC (see conftest)
    QThreadPool.globalInstance().waitForDone(10000)
    menu.hide()
    return menu, (at[0] - menu.pos().x(), at[1] - menu.pos().y())


@linux_only
def test_menu_wallpaper_backdrop_is_not_inpainted(qapp, lx, monkeypatch):
    from dragshift import radial
    wall = Image.new("RGB", (radial.SIZE, radial.SIZE), (10, 120, 200))
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: wall)
    menu, cur = _popup(radial, (300, 300))
    assert menu._raw_backdrop.getpixel(cur)[:3] == (10, 120, 200)
    assert menu.backdrop is not None


@linux_only
def test_menu_capture_is_inpainted_under_the_cursor(qapp, lx, monkeypatch):
    from PIL import ImageDraw
    from dragshift import radial

    def capture(x, y, w, h, exclude_self=False):   # blue screen with a red "drag icon" on the cursor
        im = Image.new("RGB", (w, h), (10, 120, 200))
        ImageDraw.Draw(im).rectangle((300 - x - 20, 300 - y - 20, 300 - x + 20, 300 - y + 20), fill=(255, 0, 0))
        return im

    monkeypatch.setattr(lx, "capture", capture)
    menu, cur = _popup(radial, (300, 300))
    r, g, b = menu._raw_backdrop.getpixel(cur)[:3]
    assert r < 128 and b > 128, (r, g, b)


# ---------------------------------------------------------------- toasts / settings
@linux_only
def test_reveal_uses_module_when_it_can(lx, monkeypatch, tmp_path):
    from dragshift import toasts
    spawned, asked = [], []
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    monkeypatch.setattr(lx, "reveal", lambda p: asked.append(p) or True)
    toasts.reveal(tmp_path / "out.png")
    assert asked == [str(tmp_path / "out.png")] and spawned == []


@linux_only
def test_autostart_dispatch(lx, monkeypatch):
    from dragshift import settings
    seen = []
    monkeypatch.setattr(lx, "autostart_enabled", lambda: True)
    monkeypatch.setattr(lx, "set_autostart", seen.append)
    assert settings.autostart_enabled() is True
    settings.set_autostart(True)
    settings.set_autostart(False)
    assert seen == [True, False]
