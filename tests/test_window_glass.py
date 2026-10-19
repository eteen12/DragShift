"""Glass window and toast backdrops on Linux (#40), offscreen with a fake shell extension.

The Linux branches are switched on with the modules' IS_LINUX flags, and the extension
calls (window_frame, pointer_state, work_area, place_window) and glass.grab_screen are
fakes. Those tests run on Linux only: on macOS the shared code takes its darwin branch
first (platform_mac.window_number on an offscreen window crashes), so faking Linux there
tests nothing real. The client, the D-Bus interface and the unchanged Windows/macOS path
are tested everywhere. The real thing runs in tests/linux/test_window_glass.py."""
import gc
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QRect, QThreadPool

from dragshift import glass, platform_linux, toasts, window

EXT = Path(__file__).resolve().parent.parent / "extension" / "dragshift@eteen12.github.io"
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="exercises the Linux branches of the shared code")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _renders_done(qapp):
    """Let in-flight captures and liquid renders finish and deliver, before any window
    goes (#85). A delivered capture starts a render, hence twice."""
    for _ in range(2):
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
    by title), and fakes of the captures that record (x, y, w, h, own windows left out):
    glass.grab_screen (the GUI thread's) and platform_linux.capture (a refresh's, on a
    pool thread: `threads` records where it ran; it waits while `gate` is clear and
    answers None while `answers` is False). The wallpaper is a fake too."""
    for mod in (glass, window, toasts):
        monkeypatch.setattr(mod, "IS_LINUX", True)

    class Ext:
        available = True
        answers = True
        frames: dict = {}
        grabs: list = []
        polls: list = []

    e = Ext()
    e.frames, e.grabs, e.polls, e.threads, e.walls = {}, [], [], [], []
    e.gate = threading.Event()
    e.gate.set()

    def window_frame(title):
        e.polls.append(title)
        return e.frames.get(title) if e.available else None

    def grab_screen(x, y, w, h, below_window=0):
        e.grabs.append((x, y, w, h, below_window))
        return Image.new("RGB", (w, h), (40 + len(e.grabs), 90, 160))

    def capture(x, y, w, h, exclude_self=False, worker=False):
        assert worker and exclude_self
        e.threads.append(threading.get_ident())
        e.grabs.append((x, y, w, h, int(exclude_self)))
        n = len(e.grabs)
        assert e.gate.wait(10)
        return Image.new("RGB", (w, h), (40 + n, 90, 160)) if e.answers else None

    def wallpaper_crop(x, y, w, h):
        e.walls.append(((x, y, w, h), threading.get_ident()))
        return Image.new("RGB", (w, h), (200, 100, 0))

    monkeypatch.setattr(platform_linux, "capture", capture)
    monkeypatch.setattr(platform_linux, "wallpaper_crop", wallpaper_crop)

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


def _spin(ms):
    """Run the event loop for `ms`. Not QTest.qWait: it keeps the GIL while it waits, so
    a render or capture on a pool thread crawls (a 20 ms render took 2.5 s)."""
    deadline = time.monotonic() + ms / 1000
    while True:
        QCoreApplication.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def _until(pred, ms=10000):
    deadline = time.monotonic() + ms / 1000
    while not pred():
        if time.monotonic() > deadline:
            return pred()
        _spin(10)
    return True


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
    _spin(60)
    ext.frames[w.windowTitle()] = (500, 200, 420, 320)
    assert _until(lambda: len(ext.grabs) == 2)
    _spin(400)
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
    _spin(300)
    assert ext.grabs == [] and ext.polls == [] and not w._track_timer.isActive()
    assert w.liquid.image is None and not w.liquid.excluded
    w.resize(460, 320)
    _spin(200)
    assert ext.grabs == []
    w.grab()                                          # paints the frost without raising


@linux_only
def test_frame_poll_backs_off_while_unknown(ext, qapp):
    w = window.GlassWindow("Hidden", size=(300, 200))
    w.present()                                       # the fake never reports a frame
    _spin(700)
    assert 3 <= len(ext.polls) <= 6                   # 50, 100, 200, 400 ms, ... not every 100
    assert w._track_timer.interval() > window.TRACK_MS
    assert ext.grabs == []


# ---------------------------------------------------------------- capture off the GUI thread (#107)
def _shown(ext, qapp, title="Crop", frame=(300, 120, 420, 320)):
    """A glass window that has its first backdrop; renders recorded in `w.renders`."""
    w = window.GlassWindow(title, size=frame[2:])
    ext.frames[w.windowTitle()] = frame
    w.present()
    assert _until(lambda: w.liquid.image is not None)
    _renders_done(qapp)
    return w


@pytest.fixture
def renders(monkeypatch):
    """Every backdrop render started: (window title, rect, first pixel of the image)."""
    out = []
    real = glass.LiquidBacking._render

    def render(self, pil, rect):
        out.append((self.w.windowTitle(), rect.getRect(), pil.getpixel((0, 0))))
        return real(self, pil, rect)

    monkeypatch.setattr(glass.LiquidBacking, "_render", render)
    return out


@linux_only
def test_refresh_captures_on_a_pool_thread(ext, qapp, monkeypatch):
    from dragshift import app
    logged = []
    monkeypatch.setattr(app, "log_line", logged.append)
    w = _shown(ext, qapp)
    assert ext.threads[0] != threading.get_ident()
    ext.gate.clear()                                  # the shell is slow to answer
    image = w.liquid.image
    t = time.perf_counter()
    w.liquid.refresh()
    assert time.perf_counter() - t < 0.05             # back at once (it was a blocking call)
    assert _until(lambda: len(ext.grabs) == 2)        # the capture is under way...
    _spin(100)                                  # ...and the GUI thread runs meanwhile
    assert w.liquid.image is image
    ext.gate.set()
    assert _until(lambda: w.liquid.image is not image)
    assert all(t != threading.get_ident() for t in ext.threads)
    _renders_done(qapp)
    assert logged[-1].startswith("liquid: refreshed 420x320 at (300, 120) from capture: "
                                 "capture ") and "(pool thread), GUI thread " in logged[-1]


@linux_only
def test_a_capture_overtaken_by_a_newer_refresh_is_dropped(ext, qapp, renders):
    w = _shown(ext, qapp)
    title, n = w.windowTitle(), len(renders)
    ext.gate.clear()
    ext.frames[title] = (310, 120, 420, 320)
    w.liquid.refresh()
    assert _until(lambda: len(ext.grabs) == 2)        # the first one is capturing...
    ext.frames[title] = (500, 200, 420, 320)
    w.liquid.refresh()                                # ...when the window moves again
    assert _until(lambda: len(ext.grabs) == 3)
    ext.gate.set()
    _renders_done(qapp)
    _spin(50)
    _renders_done(qapp)
    assert renders[n:] == [(title, (500, 200, 420, 320), (43, 90, 160))]   # the newer only


@linux_only
def test_a_capture_overtaken_before_it_started_is_skipped(ext, qapp, renders):
    w = _shown(ext, qapp)
    title, n = w.windowTitle(), len(renders)
    pool = QThreadPool.globalInstance()
    threads = pool.maxThreadCount()
    pool.setMaxThreadCount(1)                         # captures queue up behind each other
    try:
        ext.gate.clear()
        for x in (310, 320, 330):
            ext.frames[title] = (x, 120, 420, 320)
            w.liquid.refresh()
            assert _until(lambda: len(ext.grabs) == 2)   # 310 is capturing, the rest wait
        ext.gate.set()
        _renders_done(qapp)
    finally:
        pool.setMaxThreadCount(threads)
    assert [g[0] for g in ext.grabs[1:]] == [310, 330]   # 320 never captured
    assert [r[1][0] for r in renders[n:]] == [330]


@linux_only
def test_window_closed_while_capturing(ext, qapp, renders, capfd):
    w = _shown(ext, qapp)
    n = len(renders)
    ext.gate.clear()
    w.liquid.refresh()
    assert _until(lambda: len(ext.grabs) == 2)
    w.close()
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    ext.gate.set()
    _renders_done(qapp)
    assert renders[n:] == []
    assert "Traceback" not in capfd.readouterr().err


@linux_only
def test_hidden_while_capturing_drops_the_capture(ext, qapp, renders):
    w = _shown(ext, qapp)
    n = len(renders)
    ext.gate.clear()
    w.liquid.refresh()
    assert _until(lambda: len(ext.grabs) == 2)
    w.hide()
    ext.gate.set()
    _renders_done(qapp)
    assert renders[n:] == []


@linux_only
def test_no_capture_falls_back_to_the_wallpaper_on_the_gui_thread(ext, qapp, monkeypatch):
    from dragshift import app
    logged = []
    monkeypatch.setattr(app, "log_line", logged.append)
    ext.answers = False                               # e.g. the extension timed out
    w = _shown(ext, qapp)
    assert ext.walls == [((300, 120, 420, 320), threading.get_ident())]
    assert glass.LAST_SOURCE == "wallpaper"
    assert w.liquid.image.pixelColor(210, 160).red() > 100      # orange, not the fake capture
    assert logged[-1].startswith("liquid: refreshed 420x320 at (300, 120) from wallpaper")


@linux_only
def test_worker_capture_has_its_own_connection(monkeypatch):
    """capture(worker=True) never holds the connection the GUI thread's calls use: a
    GetPointer poll answers while a slow capture is under way."""
    import io
    from types import SimpleNamespace

    import jeepney.io.blocking
    from jeepney import MessageType
    monkeypatch.setattr(platform_linux, "_MAIN", platform_linux._Connection())
    monkeypatch.setattr(platform_linux, "_WORKER", platform_linux._Connection())
    png = io.BytesIO()
    Image.new("RGB", (8, 4), (1, 2, 3)).save(png, "PNG")
    entered, release, opened = threading.Event(), threading.Event(), []

    class Conn:
        def __init__(self):
            opened.append(self)
            self.methods = []

        def send_and_get_reply(self, msg, timeout):
            method = msg.header.fields[3]           # HeaderFields.member
            self.methods.append(method)
            if method.startswith("CaptureArea"):
                entered.set()
                assert release.wait(5)
                body = (0, 0, 4, 2, [png.getvalue()])
            else:
                body = (10, 20, 0)
            return SimpleNamespace(header=SimpleNamespace(
                message_type=MessageType.method_return, fields={}), body=body)

        def close(self):
            pass

    monkeypatch.setattr(jeepney.io.blocking, "open_dbus_connection", lambda **kw: Conn())
    result = []
    t = threading.Thread(target=lambda: result.append(
        platform_linux.capture(0, 0, 4, 2, exclude_self=True, worker=True)))
    t.start()
    try:
        assert entered.wait(5)
        t0 = time.perf_counter()
        assert platform_linux._call("GetPointer") == (10, 20, 0)
        assert time.perf_counter() - t0 < 0.5 and t.is_alive()   # didn't wait for the capture
    finally:
        release.set()
        t.join(5)
    assert result[0].size == (4, 2) and result[0].getpixel((0, 0)) == (1, 2, 3)
    assert [c.methods for c in opened] == [["CaptureAreaExcludingCaller"], ["GetPointer"]]
    assert platform_linux._WORKER.bus is opened[0] and platform_linux._MAIN.bus is opened[1]


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
    assert _until(lambda: len(ext.grabs) == 3)
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
