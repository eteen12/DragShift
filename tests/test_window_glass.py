"""Glass window and toast backdrops on Linux (#40), offscreen with a fake shell extension.

The Linux branches are switched on with the modules' IS_LINUX flags, and the extension
calls (window_frame, pointer_state, work_area, place_window) and glass.grab_screen are
fakes. Those tests run on Linux only: on macOS the shared code takes its darwin branch
first (platform_mac.window_number on an offscreen window crashes), so faking Linux there
tests nothing real. The client, the D-Bus interface and the unchanged Windows/macOS path
are tested everywhere. The real thing runs in tests/linux/test_window_glass.py."""
import gc
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QRect, QThreadPool
from PySide6.QtTest import QTest

from dragshift import glass, platform_linux, toasts, window

EXT = Path(__file__).resolve().parent.parent / "extension" / "dragshift@eteen12.github.io"
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="exercises the Linux branches of the shared code")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _renders_done(qapp):
    """Let in-flight liquid renders finish and deliver, before any window goes (#85)."""
    QThreadPool.globalInstance().waitForDone()
    qapp.processEvents()


def _close(qapp, *widgets):
    _renders_done(qapp)
    for w in widgets:
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    _renders_done(qapp)


@pytest.fixture
def ext(qapp, monkeypatch):
    """Linux branches on; a fake extension that knows where each window is (`frames`,
    by title) and a grab_screen that records (x, y, w, h, below_window)."""
    for mod in (glass, window, toasts):
        monkeypatch.setattr(mod, "IS_LINUX", True)

    class Ext:
        available = True
        frames: dict = {}
        grabs: list = []
        polls: list = []

    e = Ext()
    e.frames, e.grabs, e.polls = {}, [], []

    def window_frame(title):
        e.polls.append(title)
        return e.frames.get(title) if e.available else None

    def grab_screen(x, y, w, h, below_window=0):
        e.grabs.append((x, y, w, h, below_window))
        return Image.new("RGB", (w, h), (40 + len(e.grabs), 90, 160))

    monkeypatch.setattr(platform_linux, "window_frame", window_frame)
    monkeypatch.setattr(platform_linux, "pointer_state",
                        lambda: (800, 450, False, 0) if e.available else None)
    monkeypatch.setattr(platform_linux, "work_area",
                        lambda x, y: (66, 32, 1534, 868) if e.available else None)
    monkeypatch.setattr(platform_linux, "place_window", lambda *a: e.available)
    monkeypatch.setattr(glass, "exclude_from_capture", lambda wid: e.available)
    monkeypatch.setattr(glass, "grab_screen", grab_screen)
    yield e
    _close(qapp, *[w for w in qapp.topLevelWidgets()
                   if isinstance(w, (window.GlassWindow, toasts.ToastCard))])


def _until(pred, ms=10000):
    for _ in range(ms // 10):
        if pred():
            return True
        QTest.qWait(10)
    return pred()


# ---------------------------------------------------------------- client and interface
def test_window_frame_client(monkeypatch):
    calls = []
    replies = iter([(True, 66, 32, 820, 680), (False, 0, 0, 0, 0), None, (True, 1, 2, 0, 5)])

    def call(*a, **kw):
        calls.append(a)
        return next(replies)

    monkeypatch.setattr(platform_linux, "_call", call)
    assert platform_linux.window_frame("Crop — DragShift") == (66, 32, 820, 680)
    assert platform_linux.window_frame("x") is None       # not mapped
    assert platform_linux.window_frame("x") is None       # no extension
    assert platform_linux.window_frame("x") is None       # empty frame
    assert calls[0] == ("GetOwnWindowFrame", "s", "Crop — DragShift")


def test_get_own_window_frame_interface():
    (iface,) = ET.parse(EXT / "dbus.xml").getroot().findall("interface")
    m = iface.find("method[@name='GetOwnWindowFrame']")
    assert [(a.get("type"), a.get("direction")) for a in m.findall("arg")] == \
        [("s", "in"), ("b", "out")] + [("i", "out")] * 4
    js = (EXT / "extension.js").read_text()
    block = js[js.index("GetOwnWindowFrame (#40)"):js.index("end GetOwnWindowFrame")]
    assert "GetOwnWindowFrameAsync([title], invocation)" in block
    assert "this._callers.serve(invocation, pid =>" in block and "_windowActorsOf(pid)" in block
    assert "GetOwnWindowFrame" in (EXT / "caller.js").read_text()   # listed as guarded


# ---------------------------------------------------------------- GlassWindow
@linux_only
def test_backdrop_comes_from_where_the_shell_put_the_window(ext, qapp, monkeypatch):
    from dragshift import app
    logged = []
    monkeypatch.setattr(app, "log_line", logged.append)
    w = window.GlassWindow("Crop", size=(420, 320))
    ext.frames[w.windowTitle()] = (300, 120, 420, 320)
    w.present()
    assert ext.grabs == []                            # nothing before Mutter placed it
    assert _until(lambda: ext.grabs)
    assert ext.grabs[0] == (300, 120, 420, 320, 1)    # not Qt's frameGeometry(), own window out
    assert w.frameGeometry().topLeft().toTuple() != (300, 120)
    assert _until(lambda: w.liquid.image is not None)
    QThreadPool.globalInstance().waitForDone()
    qapp.processEvents()
    assert [ln[:44] for ln in logged] == ["liquid: refreshed 420x320 at (300, 120) from"]

    # a move (Qt gets no event for it under Wayland): one refresh once it has settled
    ext.frames[w.windowTitle()] = (350, 120, 420, 320)
    QTest.qWait(60)
    ext.frames[w.windowTitle()] = (500, 200, 420, 320)
    assert _until(lambda: len(ext.grabs) == 2)
    QTest.qWait(400)
    assert ext.grabs[1:] == [(500, 200, 420, 320, 1)]


@linux_only
def test_resize_refreshes_at_the_new_size(ext, qapp):
    w = window.GlassWindow("Resize me", size=(420, 320))
    ext.frames[w.windowTitle()] = (300, 120, 420, 320)
    w.present()
    assert _until(lambda: ext.grabs)
    w.resize(500, 360)
    ext.frames[w.windowTitle()] = (300, 120, 500, 360)
    assert _until(lambda: len(ext.grabs) == 2)
    assert ext.grabs[1] == (300, 120, 500, 360, 1)


@linux_only
def test_titles_are_unique_while_open(ext, qapp):
    a, b = window.GlassWindow("Crop Image"), window.GlassWindow("Crop Image")
    assert (a.windowTitle(), b.windowTitle()) == ("Crop Image — DragShift",
                                                  "Crop Image (2) — DragShift")
    _close(qapp, a)
    c = window.GlassWindow("Crop Image")
    assert c.windowTitle() == "Crop Image — DragShift"


@linux_only
def test_without_the_extension_painted_frost_and_no_polling(ext, qapp):
    ext.available = False
    w = window.GlassWindow("Settings", size=(420, 320))
    w.present()
    QTest.qWait(300)
    assert ext.grabs == [] and ext.polls == [] and not w._track_timer.isActive()
    assert w.liquid.image is None and not w.liquid.excluded
    w.resize(460, 320)
    QTest.qWait(200)
    assert ext.grabs == []
    w.grab()                                          # paints the frost without raising


@linux_only
def test_frame_poll_backs_off_while_unknown(ext, qapp):
    w = window.GlassWindow("Hidden", size=(300, 200))
    w.present()                                       # the fake never reports a frame
    QTest.qWait(700)
    assert 3 <= len(ext.polls) <= 6                   # 50, 100, 200, 400 ms, ... not every 100
    assert w._track_timer.interval() > window.TRACK_MS
    assert ext.grabs == []


def test_other_platforms_unchanged(qapp, monkeypatch):
    for mod in (glass, window):
        monkeypatch.setattr(mod, "IS_LINUX", False)
    if sys.platform == "darwin":    # an offscreen window has no NSView to number
        from dragshift import platform_mac
        monkeypatch.setattr(platform_mac, "window_number", lambda w: 0)
    grabs = []
    monkeypatch.setattr(glass, "grab_screen",
                        lambda x, y, w, h, b=0: grabs.append((x, y, w, h, b)) or
                        Image.new("RGB", (w, h)))
    monkeypatch.setattr(glass, "exclude_from_capture", lambda wid: True)
    monkeypatch.setattr(platform_linux, "window_frame",
                        lambda t: pytest.fail("no frame polls off Linux"))
    w = window.GlassWindow("Crop", size=(420, 320))
    assert w.windowTitle() == "Crop — DragShift"
    w.present()
    g = w.frameGeometry()
    assert grabs == [(g.x(), g.y(), 420, 320, 0)]     # before show, at Qt's geometry
    w.liquid.refresh()
    assert grabs[-1][:4] == w.frameGeometry().getRect()
    _close(qapp, w)


# ---------------------------------------------------------------- toasts
@linux_only
def test_toast_refresh_after_a_slide_uses_the_placed_rect(ext, qapp):
    st = toasts.ToastStack()
    a, b = st.new(), st.new()
    slot0 = st._slot(0)
    assert ext.grabs[0] == (*slot0.getRect(), 0)      # before show: where it is placed
    a.dismiss()
    for c in st.cards:
        c.slide.setCurrentTime(c.slide.duration())    # b slid into slot 0
    assert b._at == slot0.topLeft()
    assert ext.grabs[-1] == (*slot0.getRect(), 1)     # refreshed there, own windows out
    assert b.stage_rect() == QRect(slot0.topLeft(), b.size())
    b.dismiss()


@linux_only
def test_toast_without_the_extension_is_frost(ext, qapp):
    ext.available = False
    st = toasts.ToastStack()
    c = st.new()
    assert ext.grabs == [] and c.liquid.image is None
    c.dismiss()
