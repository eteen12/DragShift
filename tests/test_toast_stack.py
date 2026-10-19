"""Toast placement and stacking (#42), offscreen with a fake shell extension.

The Linux branch of ToastStack/ToastCard is switched on with `toasts.IS_LINUX`, and the
extension calls (`pointer_state`, `work_area`, `place_window`) are fakes, so these run on
every platform. The real thing runs in tests/linux/test_toasts.py."""
import gc

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QThreadPool

from dragshift import platform_linux, toasts
from dragshift.report import Result

AREA = (0, 32, 1600, 868)            # the session's work area with the dock at the left
SECOND = (1600, 0, 1280, 984)        # a second monitor right of it, dock at its bottom


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _settle(qapp):
    gc.collect()
    QThreadPool.globalInstance().waitForDone()
    qapp.processEvents()


@pytest.fixture
def shell(qapp, monkeypatch):
    """Linux branch on, with a fake extension: one or two monitors, a movable pointer, and a
    log of every PlaceWindow (title, x, y, whether that window was visible yet)."""
    monkeypatch.setattr(toasts, "IS_LINUX", True)

    class Shell:
        pointer = (800, 450)
        areas = [AREA, SECOND]
        placed: list = []
        available = True

        def work_area(self, x, y):
            if not self.available:
                return None
            for a in self.areas:
                if a[0] <= x < a[0] + a[2] and 0 <= y < 2000:
                    return a
            return self.areas[0]

    s = Shell()
    s.placed = []
    stacks = []
    monkeypatch.setattr(platform_linux, "pointer_state",
                        lambda: (*s.pointer, False, 0) if s.available else None)
    monkeypatch.setattr(platform_linux, "work_area", s.work_area)

    def place(title, x, y):
        card = next((c for st in stacks for c in st.cards if c.windowTitle() == title), None)
        s.placed.append((title, x, y, bool(card and card.isVisible())))
        return True

    monkeypatch.setattr(platform_linux, "place_window", place)
    monkeypatch.setattr(toasts.glass, "exclude_from_capture", lambda wid: False)

    def stack():
        st = toasts.ToastStack()
        stacks.append(st)
        return st

    s.stack = stack
    yield s
    for st in stacks:
        for c in list(st.cards):
            c.dismiss()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _settle(qapp)


def _slot(area, i):
    x, y, w, h = area
    g = QRect(x, y, w, h)
    return (g.right() - toasts.W - toasts.MARGIN,
            g.bottom() - toasts.MARGIN - toasts.H - i * (toasts.H + toasts.GAP))


def _finish_slides(stack):
    for c in stack.cards:
        if c.slide.state() == c.slide.State.Running:
            c.slide.setCurrentTime(c.slide.duration())


def test_cards_stack_bottom_right_of_the_work_area(shell, qapp):
    st = shell.stack()
    cards = [st.new() for _ in range(3)]
    for i, c in enumerate(cards):
        assert c.windowTitle() == f"DragShift toast {i + 1}"
        assert (c.geometry().x(), c.geometry().y()) == _slot(AREA, i)
        assert c.geometry().size().toTuple() == (toasts.W, toasts.H)
    # 1600-380-18-1 = 1201; 32+868-1-18-104 = 777: above the bottom edge of the work area
    assert _slot(AREA, 0) == (1201, 777)
    # each card was placed once, before it was shown, at its slot
    assert [p for p in shell.placed] == [
        (f"DragShift toast {i + 1}", *_slot(AREA, i), False) for i in range(3)]


def test_at_most_four_cards_and_titles_are_reused(shell, qapp):
    st = shell.stack()
    cards = [st.new() for _ in range(5)]
    _settle(qapp)
    assert st.cards == cards[1:]
    titles = [c.windowTitle() for c in st.cards]
    assert len(set(titles)) == 4
    assert cards[4].windowTitle() == "DragShift toast 1"     # the oldest card's title freed
    _finish_slides(st)
    for i, c in enumerate(st.cards):
        assert c._at.toTuple() == _slot(AREA, i)
        assert (c.geometry().x(), c.geometry().y()) == _slot(AREA, i)


def test_dismissed_card_lets_the_ones_above_slide_down(shell, qapp):
    st = shell.stack()
    a, b, c = (st.new() for _ in range(3))
    shell.placed.clear()
    a.dismiss()
    assert st.cards == [b, c]
    assert b.slide.state() == b.slide.State.Running
    for t in (60, 120):
        for k in (b, c):
            k.slide.setCurrentTime(t)
    _finish_slides(st)
    for i, k in enumerate((b, c)):
        moves = [(x, y) for title, x, y, _ in shell.placed if title == k.windowTitle()]
        assert moves[-1] == _slot(AREA, i)
        ys = [y for _, y in moves]
        assert ys == sorted(ys) and len(ys) >= 3, moves       # slides down step by step
        assert all(x == _slot(AREA, i)[0] for x, _ in moves)
        assert (k.geometry().x(), k.geometry().y()) == _slot(AREA, i)
        assert all(visible for title, *_, visible in shell.placed if title == k.windowTitle())


def test_stack_follows_the_pointer_to_another_monitor(shell, qapp):
    st = shell.stack()
    a = st.new()
    shell.pointer = (2000, 300)
    b = st.new()
    assert (b.geometry().x(), b.geometry().y()) == _slot(SECOND, 1)
    _finish_slides(st)
    assert a._at.toTuple() == _slot(SECOND, 0)            # the older card came along
    # the dock hides: the area stays the one taken at new()
    shell.areas = [AREA, (1600, 0, 1280, 1024)]
    a.dismiss()
    _finish_slides(st)
    assert b._at.toTuple() == _slot(SECOND, 0)


def test_without_the_extension_qt_screen_is_used(shell, qapp):
    shell.available = False
    st = shell.stack()
    c = st.new()
    g = toasts.QGuiApplication.primaryScreen().availableGeometry()
    assert (c.geometry().x(), c.geometry().y()) == _slot(g.getRect(), 0)


def test_fades_by_painting_not_window_opacity(shell, qapp):
    st = shell.stack()
    c = st.new()
    assert c.fade.state() == c.fade.State.Running
    c.fade.setCurrentTime(c.fade.duration())
    assert c._fade == pytest.approx(1.0)
    assert c.windowOpacity() == 1.0             # never set: Wayland has no window opacity


def test_lifetimes_and_hover_pause(shell, qapp, tmp_path):
    st = shell.stack()
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    done = st.new()
    done.done(Result(title="Converted", detail="a.png → a.jpg", outs=[out]))
    err = st.error("Couldn't finish", "boom")
    info = st.info("Hi", "there")
    assert [c.life.interval() for c in (done, err, info)] == [6500, 9000, 4500]
    done.enterEvent(None)
    assert not done.life.isActive()             # paused while the pointer is on it
    done.leaveEvent(None)
    assert done.life.isActive() and done.life.interval() == 2500


def test_other_platforms_unchanged(qapp, monkeypatch):
    monkeypatch.setattr(toasts, "IS_LINUX", False)
    monkeypatch.setattr(toasts.glass, "exclude_from_capture", lambda wid: False)
    called = []
    monkeypatch.setattr(platform_linux, "place_window", lambda *a: called.append(a))
    monkeypatch.setattr(platform_linux, "work_area", lambda *a: called.append(a))
    st = toasts.ToastStack()
    a, b = st.new(), st.new()
    g = toasts.QGuiApplication.screenAt(toasts.QCursor.pos()) or \
        toasts.QGuiApplication.primaryScreen()
    g = g.availableGeometry()
    assert (b.geometry().x(), b.geometry().y()) == _slot(g.getRect(), 1)
    assert b.windowTitle() == "" and a.windowOpacity() == 0.0
    a.dismiss()
    assert (b.geometry().x(), b.geometry().y()) == _slot(g.getRect(), 0)   # at once
    assert called == []
    b.dismiss()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _settle(qapp)


# ---------------------------------------------------------------- GetWorkArea interface
def test_get_work_area_interface_matches_the_client():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    ext = Path(__file__).resolve().parent.parent / "extension" / "dragshift@eteen12.github.io"
    (iface,) = ET.parse(ext / "dbus.xml").getroot().findall("interface")
    m = iface.find("method[@name='GetWorkArea']")
    assert [(a.get("type"), a.get("direction")) for a in m.findall("arg")] == \
        [("i", "in")] * 2 + [("i", "out")] * 4
    assert "GetWorkAreaAsync([x, y], invocation)" in (ext / "extension.js").read_text()


def test_work_area_is_none_without_the_extension():
    # tests/conftest.py points the session bus at a socket that does not exist
    assert platform_linux.work_area(10, 10) is None
