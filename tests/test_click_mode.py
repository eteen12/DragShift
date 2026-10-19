"""Click mode on Linux (#36), offscreen: the menu opened from the tray, Open With or a second
launch, with the shell extension faked.

On GNOME Wayland the menu never holds the keyboard focus and grabMouse() never sees a click
on another app, so while a click-mode menu is open it holds the extension's Escape watch
and a thread polls the pointer state for a button pressed outside it. The real thing, with injected
input in the isolated session, is tests/linux/test_click_mode.py."""
import gc
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux click mode")

LEFT, RIGHT, SHIFT = 1 << 8, 1 << 9, 1 << 0


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ext(qapp, monkeypatch):
    """The extension, faked: pointer_state() answers `ext.pointer` (None = no extension),
    watch_escape() records its calls and keeps the callback in `ext.escape`."""
    from dragshift import platform_linux as lx
    rec = SimpleNamespace(pointer=(500, 400, False, 0), calls=[], escape=None, grant=True)

    def watch_escape(on, callback=None):
        rec.calls.append(on)
        rec.escape = callback if on else None
        return on and rec.grant

    monkeypatch.setattr(lx, "pointer_state", lambda: rec.pointer)
    monkeypatch.setattr(lx, "watch_escape", watch_escape)
    monkeypatch.setattr(lx, "capture", lambda x, y, w, h, exclude_self=False:
                        Image.new("RGB", (w, h), (10, 120, 200)))
    monkeypatch.setattr(lx, "place_window", lambda *a: False)
    monkeypatch.setattr(lx, "work_area", lambda x, y: None)
    return rec


@pytest.fixture
def menu(ext):
    from PySide6.QtCore import QThreadPool
    from dragshift import radial
    m = radial.RadialMenu(lambda p: {"Convert": [radial.Item("PNG"), radial.Item("JPG")],
                                     "Tools": [radial.Item("Compress")]},
                          lambda p: "1 KB")
    m.grabbed = 0
    m.grabMouse = lambda: setattr(m, "grabbed", m.grabbed + 1)
    m.closed = []
    m.dismissed.connect(lambda: m.closed.append(True))
    yield m
    gc.collect()
    QThreadPool.globalInstance().waitForDone(10000)
    m.hide()


def _open(menu, at=(500, 400)):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    menu.popup(QPoint(*at), ["/tmp/x.png"])
    QApplication.processEvents()          # grabMouse would be queued with singleShot(0)


@pytest.fixture
def looks(menu, ext, monkeypatch):
    """The pointer watch driven by hand: its thread does nothing, and looks(x, y, mask)
    is one look at the pointer, as the thread makes it (its signals arrive at once)."""
    from dragshift import radial
    monkeypatch.setattr(radial.RadialMenu, "_linux_watch", lambda self, serial, stop: None)
    state = {"held": None}

    def look(x, y, mask):
        ext.pointer = (x, y, bool(mask & LEFT), mask)
        state["held"] = menu._linux_poll(menu._watch_serial, state["held"])
    return look


def test_click_mode_watches_escape_and_the_pointer_instead_of_grabbing(menu, ext):
    _open(menu)
    assert menu.click_mode and menu.isVisible()
    watch = menu._watch
    assert ext.calls == [True] and watch is not None and not watch.is_set()
    assert menu.grabbed == 0                  # grabMouse never sees other apps' clicks
    _open(menu)                               # opened again while open: still one watch
    assert ext.calls == [True] and menu._watch is watch
    menu.hide()
    assert ext.calls == [True, False] and watch.is_set() and menu._watch is None


def test_the_watch_thread_closes_the_menu_on_a_press_outside(menu, ext):
    """The real thread: it reads the pointer every CLICK_POLL_MS and the close happens
    on the GUI thread."""
    import time
    from PySide6.QtWidgets import QApplication
    from dragshift import radial
    _open(menu)
    names = [t.name for t in threading.enumerate()]
    assert "menu-click-watch" in names
    time.sleep(3 * radial.CLICK_POLL_MS / 1000)          # it has seen nothing held
    ext.pointer = (100, 100, True, LEFT)
    deadline = time.monotonic() + 2
    while menu.isVisible() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    assert not menu.isVisible() and menu.closed == [True]
    time.sleep(3 * radial.CLICK_POLL_MS / 1000)
    assert "menu-click-watch" not in [t.name for t in threading.enumerate()]


def test_a_stale_watch_cannot_close_the_next_menu(menu, ext, looks):
    _open(menu)
    old = menu._watch_serial
    menu.hide()
    _open(menu)
    menu._linux_outside(old, 100, 100)        # queued by the old thread before it stopped
    menu._linux_shift(old)
    assert menu.isVisible() and menu.ring_idx == 0


def test_a_button_pressed_outside_closes_the_menu(menu, ext, looks):
    _open(menu)                               # frame (300, 200)-(699, 599)
    looks(100, 100, 0)
    looks(100, 100, LEFT)
    assert menu.closed == [True] and not menu.isVisible()
    assert ext.calls == [True, False]         # Escape released on close


@pytest.mark.parametrize("button", [LEFT, RIGHT], ids=["left", "right"])
def test_presses_on_the_menu_are_left_to_qt(menu, ext, looks, button):
    """A press inside the frame is the menu's own click (mouseReleaseEvent decides)."""
    _open(menu)
    for x, y in ((500, 400), (300, 200), (699, 599)):
        looks(x, y, 0)
        looks(x, y, button)
    assert menu.isVisible() and not menu.closed


def test_a_button_held_when_the_menu_opens_counts_only_when_pressed_again(menu, ext, looks):
    """E.g. the click that chose "Open a file…" may still be down."""
    _open(menu)
    looks(100, 100, LEFT)                     # first look: held already
    looks(120, 100, LEFT)
    assert menu.isVisible()
    looks(120, 100, 0)
    looks(120, 100, RIGHT)                    # pressed again (another button)
    assert not menu.isVisible()


def test_no_pointer_state_keeps_the_menu_open(menu, ext, looks):
    _open(menu)
    ext.pointer = None
    assert menu._linux_poll(menu._watch_serial, None) is None
    assert menu._linux_poll(menu._watch_serial, 0) == 0
    assert menu.isVisible()


def test_shift_going_down_flips_the_ring(menu, ext, looks):
    _open(menu)
    looks(500, 400, 0)
    looks(500, 400, SHIFT)
    assert menu.ring_names[menu.ring_idx] == "Tools"
    looks(500, 400, SHIFT)         # still held: no second flip
    assert menu.ring_names[menu.ring_idx] == "Tools"
    assert menu.isVisible()


def test_escape_from_the_extensions_thread_closes_the_menu(menu, ext):
    from PySide6.QtWidgets import QApplication
    _open(menu)
    t = threading.Thread(target=ext.escape)   # the jeepney receiver thread calls it
    t.start()
    t.join()
    assert menu.isVisible()                   # handed to the GUI thread, not run there
    QApplication.processEvents()
    assert menu.closed == [True] and ext.calls == [True, False]


def test_escape_unavailable_still_leaves_outside_clicks(menu, ext, looks, capsys):
    ext.grant = False                         # no extension, or another program holds it
    _open(menu)
    assert "Escape not watched" in capsys.readouterr().out
    looks(100, 100, 0)
    looks(100, 100, LEFT)
    assert not menu.isVisible()
    assert ext.calls == [True]                # nothing held, nothing to release


def test_a_drag_menu_replacing_a_click_menu_drops_the_watch(menu, ext):
    from PySide6.QtCore import QPoint
    _open(menu)
    watch = menu._watch
    menu.popup(QPoint(600, 400))              # the trigger: Shift+drag while it is open
    assert not menu.click_mode
    assert ext.calls == [True, False] and watch.is_set() and menu._watch is None
    menu.hide()
    assert ext.calls == [True, False]


def test_click_mode_position_is_the_extensions_pointer_else_the_screen_centre(qapp, ext):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    from dragshift import app
    ext.pointer = (812, 345, False, 0)
    assert app.linux_click_pos() == QPoint(812, 345)
    ext.pointer = None
    assert app.linux_click_pos() == QGuiApplication.primaryScreen().geometry().center()


def test_show_click_menu_opens_at_the_pointer_without_activating(qapp, ext):
    from PySide6.QtCore import QPoint
    from dragshift import app
    calls = []
    fake = SimpleNamespace(menu=SimpleNamespace(
        popup=lambda pos, files: calls.append(("popup", pos, files)),
        activateWindow=lambda: calls.append(("activate",))))
    ext.pointer = (812, 345, False, 0)
    app.DragShiftApp.show_click_menu(fake, ["/tmp/a.png"])
    assert calls == [("popup", QPoint(812, 345), ["/tmp/a.png"])]
