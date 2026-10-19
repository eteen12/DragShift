"""Closing the drag-mode menu on a cancelled drag on Linux (#32), offscreen, with the shell
extension faked.

Escape during a file drag never reaches DragShift on GNOME: Mutter takes it and cancels the
drag, and the menu only sees a dragLeave while the button is still down and the pointer is
still on it (#24). RadialMenu records when the drag left; DragShiftWatcher closes the menu
once that has lasted DRAG_CANCEL_GRACE with no drop or re-entry. The real thing, with real
drags and injected Escape in the isolated session, is tests/linux/test_menu_cancel.py."""
import gc
import sys
import time

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux menu")

SHIFT = 1 << 0
AT = (500, 400)               # where the menu opens; its frame is 300..700 x 200..600


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ext(qapp, monkeypatch):
    """pointer_state() answers ext[0]; capture and placement are faked."""
    from dragshift import platform_linux as lx
    ext = [(AT[0], AT[1], True, SHIFT)]
    monkeypatch.setattr(lx, "pointer_state", lambda: ext[0])
    monkeypatch.setattr(lx, "extension_available", lambda: True)
    monkeypatch.setattr(lx, "capture", lambda x, y, w, h, exclude_self=False:
                        Image.new("RGB", (w, h), (10, 120, 200)))
    monkeypatch.setattr(lx, "place_window", lambda *a: False)
    monkeypatch.setattr(lx, "work_area", lambda x, y: None)
    return ext


@pytest.fixture
def menu(ext):
    from PySide6.QtCore import QThreadPool
    from dragshift import radial
    m = radial.RadialMenu(lambda p: {"Convert": [radial.Item("PNG")]}, lambda p: "1 KB")
    m.closed = []
    m.dismissed.connect(lambda: m.closed.append(True))
    yield m
    gc.collect()
    QThreadPool.globalInstance().waitForDone(10000)
    m.hide()


def _popup(menu):
    from PySide6.QtCore import QPoint
    menu.popup(QPoint(*AT))
    assert menu.isVisible() and not menu.click_mode


def _enter(menu, path="/tmp/Photo.jpg"):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(path)])
    e = QDragEnterEvent(QPointF(200, 200), Qt.CopyAction | Qt.MoveAction, md, Qt.LeftButton,
                        Qt.ShiftModifier)
    menu.dragEnterEvent(e)
    return e


def _leave(menu):
    from PySide6.QtGui import QDragLeaveEvent
    menu.dragLeaveEvent(QDragLeaveEvent())


def _p(x, y):
    from PySide6.QtCore import QPoint
    return QPoint(x, y)


def test_leave_after_a_file_entered_is_recorded(menu):
    _popup(menu)
    assert menu.drag_cancelled_at is None
    _enter(menu)
    assert menu.got_drag and menu.drag_cancelled_at is None
    t = time.monotonic()
    _leave(menu)
    assert menu.drag_cancelled_at is not None and menu.drag_cancelled_at >= t
    assert menu.drag_cancelled_since(_p(*AT)) == menu.drag_cancelled_at     # still on it
    assert menu.drag_cancelled_since(_p(301, 201)) == menu.drag_cancelled_at
    assert menu.drag_cancelled_since(_p(299, 400)) is None                  # carried out
    assert menu.drag_cancelled_since(_p(500, 600)) is None


def test_entering_again_or_a_new_menu_clears_it(menu):
    _popup(menu)
    _enter(menu)
    _leave(menu)
    _enter(menu)
    assert menu.drag_cancelled_since(_p(*AT)) is None
    _leave(menu)
    _popup(menu)
    assert menu.drag_cancelled_at is None and not menu.got_drag


def test_a_leave_without_a_file_records_nothing(menu):
    _popup(menu)
    _leave(menu)
    assert menu.drag_cancelled_at is None


def test_watcher_closes_the_real_menu_on_a_cancel(menu, ext, monkeypatch):
    """The two wired as app.py wires them: file enters, leaves with the button still down
    and the pointer on the menu; after the grace the next tick closes it."""
    from dragshift import trigger
    w = trigger.DragShiftWatcher(lambda: menu.isVisible() and not menu.click_mode,
                                 lambda: menu.got_drag, None, menu.drag_cancelled_since)
    w.hide_menu.connect(menu.close_menu)
    w._shown_at = _p(*AT)
    _popup(menu)
    _enter(menu)
    w._tick()
    _leave(menu)
    w._tick()
    assert menu.isVisible()                   # within the grace: a drop may still come
    time.sleep(trigger.DRAG_CANCEL_GRACE + 0.01)
    w._tick()
    assert not menu.isVisible() and menu.closed == [True]


def test_watcher_keeps_the_menu_when_the_drag_is_carried_out(menu, ext):
    from dragshift import trigger
    w = trigger.DragShiftWatcher(lambda: menu.isVisible() and not menu.click_mode,
                                 lambda: menu.got_drag, None, menu.drag_cancelled_since)
    w.hide_menu.connect(menu.close_menu)
    w._shown_at = _p(*AT)
    _popup(menu)
    _enter(menu)
    _leave(menu)
    ext[0] = (800, 400, True, SHIFT)          # out of the menu, button still down
    time.sleep(trigger.DRAG_CANCEL_GRACE + 0.01)
    w._tick()
    assert menu.isVisible()
