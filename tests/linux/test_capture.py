"""CaptureArea in the shipped extension and its client platform_linux.capture(): the
screen under a rect, without the drag icon, as a PIL image of exactly the asked size.

Session tests load the extension only inside the isolated session from
tests/linux/session.py. The client runs in this process against that session's private
bus (DBUS_SESSION_BUS_ADDRESS is pointed at it for the test), never the real desktop's.
The drag is a real one, from Nautilus (tests/linux/files.py), driven by tests/linux/inject.py.
"""
import io
import os
import shutil
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jeepney.io.blocking import open_dbus_connection
from PIL import Image, ImageChops, ImageStat

from dragshift import glass, platform_linux as lx
from tests.linux import caller, inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.probe_window import ProbeWindow
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (SRC, UUID, _errors_about, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.linux.test_pointer import _reset_client

gnome = pytest.mark.gnome

SIZE = (1600, 900)
PROBE_RECT = (900, 200, 520, 440)       # right of the Nautilus window (files.NAUTILUS_RECT)
RED = (220, 30, 30)


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference per channel, 0..255."""
    assert a.size == b.size, (a.size, b.size)
    return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"),
                                                    b.convert("RGB"))).mean) / 3


def _red_pixels(im: Image.Image) -> int:
    """Pixels of the test's flat red (the dragged picture), which nothing else shows."""
    r, g, b = (ch.point(lambda v, t=t: 255 if t(v) else 0) for ch, t in
               zip(im.convert("RGB").split(), (lambda v: v > 180, lambda v: v < 80,
                                               lambda v: v < 80)))
    both = ImageChops.multiply(ImageChops.multiply(r, g), b)
    return both.histogram()[255]


def _changed(im: Image.Image, ref: Image.Image, threshold: int = 16) -> Image.Image:
    """Mask (L, 0 or 255) of the pixels where some channel of `im` differs from `ref` by
    more than `threshold`."""
    assert im.size == ref.size, (im.size, ref.size)
    r, g, b = ImageChops.difference(im.convert("RGB"), ref.convert("RGB")).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(
        lambda v: 255 if v > threshold else 0)


def _count(mask: Image.Image) -> int:
    return mask.histogram()[255]


MIN_ICON = 16       # the smallest drag icon GNOME draws is 16x16


def _icon_box(shot: Image.Image, no_drag: Image.Image, at: tuple[int, int]):
    """Bounding box of what a drag added to `shot` (a screenshot of the region) compared
    with the same region without a drag, if it looks like a drag icon: at least MIN_ICON
    each way and covering the pointer `at` (region coordinates). Else None. Whatever
    picture the icon is (a thumbnail, a placeholder, a theme's mime-type icon), only
    where it is counts."""
    box = _changed(shot, no_drag).getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    if x1 - x0 < MIN_ICON or y1 - y0 < MIN_ICON:
        return None
    if not (x0 <= at[0] < x1 and y0 <= at[1] < y1):
        return None
    return box


def _evidence(im: Image.Image, name: str) -> None:
    d = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if d:
        im.save(Path(d) / name)


# ------------------------------------------------------------------ client, no shell
@pytest.fixture
def fake(monkeypatch):
    """lx._call answering CaptureArea from a picture of the 'screen' (SIZE, a gradient),
    clamping the rect like the extension does; `scale` makes it a HiDPI screen."""
    screen = Image.linear_gradient("L").resize(SIZE).convert("RGB")
    f = SimpleNamespace(calls=[], scale=1, strips=3, reply=None, screen=screen)

    def call(method, signature, *args, timeout):
        f.calls.append((method, signature, args, timeout))
        if f.reply is not None:
            return f.reply
        x, y, w, h = args
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(SIZE[0], x + w), min(SIZE[1], y + h)
        if w <= 0 or h <= 0 or x1 <= x0 or y1 <= y0:
            return None
        im = screen.crop((x0, y0, x1, y1))
        im = im.resize((im.width * f.scale, im.height * f.scale), Image.NEAREST)
        n = min(f.strips, im.height)
        cuts = [im.height * i // n for i in range(n + 1)]
        return (x0, y0, x1 - x0, y1 - y0,
                [_png(im.crop((0, a, im.width, b))) for a, b in zip(cuts, cuts[1:])])

    monkeypatch.setattr(lx, "_call", call)
    return f


def test_capture_returns_the_rect_as_rgb(fake):
    im = lx.capture(100, 50, 400, 300)
    assert im.mode == "RGB" and im.size == (400, 300)
    assert _diff(im, fake.screen.crop((100, 50, 500, 350))) == 0
    (method, sig, args, timeout), = fake.calls
    assert (method, sig, args) == ("CaptureArea", "iiii", (100, 50, 400, 300))
    assert timeout == lx.CAPTURE_TIMEOUT


def test_one_strip_or_many(fake):
    for n in (1, 4, 7):
        fake.strips = n
        im = lx.capture(100, 50, 400, 300)
        assert im.size == (400, 300)
        assert _diff(im, fake.screen.crop((100, 50, 500, 350))) == 0, n


def test_hidpi_capture_is_scaled_to_logical_size(fake):
    fake.scale = 2
    im = lx.capture(100, 50, 400, 300)
    assert im.size == (400, 300)
    assert _diff(im, fake.screen.crop((100, 50, 500, 350))) < 1


@pytest.mark.parametrize("rect", [(-60, -40, 200, 150), (1500, 800, 200, 150),
                                  (-60, 800, 200, 150), (1500, -40, 200, 150)])
def test_clamped_rect_is_padded_with_edge_pixels(fake, rect):
    x, y, w, h = rect
    im = lx.capture(*rect)
    assert im.size == (w, h)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(SIZE[0], x + w), min(SIZE[1], y + h)
    inner = im.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    assert _diff(inner, fake.screen.crop((x0, y0, x1, y1))) == 0
    # every padded pixel repeats the nearest captured one
    for px, py in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (0, h // 2),
                   (w // 2, h - 1), (w - 1, h // 2)):
        sx = min(max(x + px, x0), x1 - 1)
        sy = min(max(y + py, y0), y1 - 1)
        assert im.getpixel((px, py)) == fake.screen.getpixel((sx, sy)), (px, py)


def test_nothing_to_capture_gives_none_without_a_call(fake):
    for rect in ((0, 0, 0, 10), (0, 0, 10, -1)):
        assert lx.capture(*rect) is None
    assert fake.calls == []


def test_failed_or_garbled_reply_gives_none(fake):
    fake.reply = None
    assert lx.capture(5000, 5000, 10, 10) is None          # off the stage: D-Bus error
    strip = _png(Image.new("RGB", (10, 5)))
    for reply in ((0, 0, 10, 10, [b"not a png"]), (0, 0, 10, 10, []),
                  (0, 0, 10, 10, [strip, _png(Image.new("RGB", (9, 5)))]),   # widths differ
                  (0, 0, 10), ()):
        fake.reply = reply
        assert lx.capture(0, 0, 10, 10) is None


def test_grab_screen_uses_the_capture(fake, monkeypatch):
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: pytest.fail("wallpaper used"))
    im = glass.grab_screen(100, 50, 400, 400)
    assert glass.LAST_SOURCE == "capture" and im.size == (400, 400)
    assert _diff(im, fake.screen.crop((100, 50, 500, 450))) == 0


def test_dbus_xml_declares_capture_area():
    import xml.etree.ElementTree as ET
    iface = ET.parse(SRC / "dbus.xml").getroot().find("interface")
    m = iface.find("method[@name='CaptureArea']")
    assert [(a.get("type"), a.get("direction")) for a in m.findall("arg")] == \
        [("i", "in")] * 4 + [("i", "out")] * 4 + [("aay", "out")]
    assert "CaptureAreaAsync(" in (SRC / "extension.js").read_text()


# ------------------------------------------------------------------ in the session
@pytest.fixture(scope="module")
def cap_session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(size=SIZE, extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(cap_session):
    with ShellHelper(cap_session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(cap_session)), _state(cap_session)
        yield helper


@pytest.fixture
def client(cap_session, shell, monkeypatch):
    """platform_linux in this process, connected to the isolated session's bus."""
    _reset_client()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", cap_session.bus_address)
    try:
        assert _wait(lambda: lx._call("Version") is not None)
        yield lx
    finally:
        shell.release_all()
        _reset_client()


@pytest.fixture
def probe(cap_session, shell):
    # "ignore": a refused drop leaves the probe's canvas as it was
    with ProbeWindow(cap_session, shell, drop_action="ignore") as p:
        p.move(*PROBE_RECT)
        yield p
    shell.release_all()


def _helper_area(s, shell, x, y, w, h, name) -> Image.Image:
    path = shell.screenshot_area(x, y, w, h, s.root / "tmp" / name)
    with Image.open(path) as im:
        return im.convert("RGB")


def _capture_reply(s, *rect):
    """CaptureArea from this process on the session's bus: ("ok", body) or (error name,
    message). (Not gdbus: the extension refuses it, #93.)"""
    conn = open_dbus_connection(bus=s.bus_address)
    try:
        return caller.call(conn, "CaptureArea", "iiii", rect)
    finally:
        conn.close()


@gnome
def test_capture_equals_the_helper_screenshot(cap_session, shell, client, probe):
    """Acceptance (1): a region over the probe window equals the helper's ScreenshotArea
    crop (mean abs diff < 2)."""
    inject.move(shell, 40, 860)                      # pointer out of the way
    time.sleep(0.3)
    fx, fy, fw, fh = probe.frame()
    rect = (fx + 20, fy + 60, 400, 300)
    im = client.capture(*rect)
    ref = _helper_area(cap_session, shell, *rect, "probe-helper.png")
    assert im.size == (400, 300) and im.mode == "RGB"
    d = _diff(im, ref)
    print(f"\ncapture vs helper over the probe: mean abs diff {d:.3f}")
    assert d < 2
    # the probe's canvas really is in it (its #f4f1e8 background), not black or wallpaper
    r, g, b = ImageStat.Stat(im).median
    assert abs(r - 0xf4) < 12 and abs(g - 0xf1) < 12 and abs(b - 0xe8) < 12, (r, g, b)
    _evidence(im, "probe-capture.png")
    _evidence(ref, "probe-helper.png")


@gnome
def test_rect_partly_off_screen_is_clamped_and_padded(cap_session, shell, client):
    inject.move(shell, 800, 450)
    rect = (SIZE[0] - 150, -50, 200, 200)            # top-right corner, 50 px off each side
    im = client.capture(*rect)
    assert im.size == (200, 200)
    ref = _helper_area(cap_session, shell, SIZE[0] - 150, 0, 150, 150, "corner.png")
    assert _diff(im.crop((0, 50, 150, 200)), ref) < 2
    for py in range(0, 50, 7):                        # top band repeats the first row
        assert im.getpixel((10, py)) == im.getpixel((10, 50))
    for px in range(150, 200, 7):                     # right band repeats the last column
        assert im.getpixel((px, 120)) == im.getpixel((149, 120))


@gnome
def test_bad_rects_are_dbus_errors(cap_session, shell, client):
    s = cap_session
    for rect in ((0, 0, 0, 100), (0, 0, 100, -5), (SIZE[0] + 10, 0, 50, 50), (-200, -200, 100, 100)):
        status, body = _capture_reply(s, *rect)
        assert status == "org.freedesktop.DBus.Error.InvalidArgs", (rect, status, body)
        assert lx.capture(*rect) is None
    status, body = _capture_reply(s, 10, 10, 4, 3)
    assert status == "ok" and body[:4] == (10, 10, 4, 3) and len(body[4]) == 1, body


@gnome
def test_big_rects_come_as_parallel_strips(cap_session, shell, client):
    for (w, h), n in (((40, 40), 1), ((400, 2), 1), ((300, 200), 2), ((400, 400), 4),
                         ((1600, 900), 4)):
        body = lx._call("CaptureArea", "iiii", 0, 0, w, h, timeout=2)
        assert body[:4] == (0, 0, w, h) and len(body[4]) == n, ((w, h), len(body[4]))
        heights = [Image.open(io.BytesIO(png)).size for png in body[4]]
        assert {sw for sw, _ in heights} == {w} and sum(sh for _, sh in heights) == h


@gnome
def test_drag_icon_is_left_out(cap_session, shell, client, probe):
    """Acceptance (2): during a real drag from Nautilus, with the drag icon inside the
    region, capture() has no icon pixels (not one pixel differs from the no-drag capture)
    while the helper's screenshot shows the icon. The helper's screenshot is taken right
    after capture(), so it also shows the icon was put back at once (no flicker).

    The icon is found as what the drag adds to the helper's screenshot, not by its
    colour: Nautilus drags the file's thumbnail (the flat red picture) where it can make
    one, and the theme's image icon where it can't (the Docker image has no thumbnailer,
    and its bubblewrap sandbox can't run in an unprivileged container; #96)."""
    s = cap_session
    big_red = _png(Image.new("RGB", (256, 192), RED))   # a big, flat red thumbnail
    with Files(s, shell) as files:
        folder = files.make_folder("capture-drag", {"Red.png": big_red})
        files.open_folder(folder)
        start = files.icon_center("Red.png", "nautilus")
        fx, fy, fw, fh = probe.frame()
        target = (fx + fw // 2, fy + 170)               # above the probe's status line
        side = 200
        rect = (target[0] - side // 2, target[1] - side // 2, side, side)
        at = (target[0] - rect[0], target[1] - rect[1])  # the pointer in the region

        inject.glide(shell, *target)                    # same place, no drag yet
        time.sleep(0.5)
        before = client.capture(*rect)
        before_helper = _helper_area(s, shell, *rect, "drag-before-helper.png")
        assert _diff(before, before_helper) < 2

        for _attempt in range(3):
            mark = probe.mark()
            inject.drag_to(shell, *target, start=start, dur=0.8, release=False)
            try:
                probe.wait_for("dragEnter", after=mark, timeout=10)
                # Wait until the icon is on screen and has stopped changing: Nautilus
                # may start with a "loading" placeholder and switch to the thumbnail.
                deadline = time.monotonic() + 3
                last = None
                while time.monotonic() < deadline:
                    shot = _helper_area(s, shell, *rect, "drag-wait.png")
                    if (_icon_box(shot, before, at) and last is not None
                            and _diff(shot, last) == 0):
                        break
                    last = shot
                    time.sleep(0.1)
                assert shell.get_pointer()[:2] == target
                during = client.capture(*rect)
                during_helper = _helper_area(s, shell, *rect, "drag-during-helper.png")
                full = shell.screenshot(s.root / "tmp" / "drag-during-full.png")
            finally:
                inject.button_up(shell)          # refused by the probe: nothing changes
                shell.release_all()
            if _icon_box(during_helper, before, at):
                break
            time.sleep(1.5)                      # the refused drag's icon flies back
        icon = _changed(during_helper, before)
        leak = _changed(during, before)
        box = _icon_box(during_helper, before, at)
        d_capture, d_helper = _diff(during, before), _diff(during_helper, before)
        red, red_capture = _red_pixels(during_helper), _red_pixels(during)
        print(f"\ndrag in progress, {side}x{side} around the pointer: icon in the helper's "
              f"screenshot {box} ({_count(icon)} changed pixels), changed pixels in "
              f"capture() {_count(leak)}; mean diff vs no-drag: capture {d_capture:.3f}, "
              f"helper {d_helper:.3f}; red pixels: helper {red}, capture {red_capture}")
        sbs = Image.new("RGB", (side * 4 + 30, side), "white")
        for i, im in enumerate((before, during, during_helper, icon.convert("RGB"))):
            sbs.paste(im, (i * (side + 10), 0))
        _evidence(sbs, "drag-side-by-side.png")
        with Image.open(full) as im:
            _evidence(im.convert("RGB"), "drag-during-full.png")
        assert box, "the drag icon is not in the helper's screenshot of the region"
        assert _count(leak) == 0, f"capture() shows {_count(leak)} pixels of the drag icon"
        assert d_capture < 3 and red_capture == 0


# A GTK 3 X11 drag icon, as GTK draws one during an X11 drag: a popup window with the
# DND type hint (_NET_WM_WINDOW_TYPE_DND), flat red, at the given place.
X11_DND_ICON = """
import sys, gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk
x, y = map(int, sys.argv[1:3])
w = Gtk.Window(type=Gtk.WindowType.POPUP)
w.set_type_hint(Gdk.WindowTypeHint.DND)
w.set_default_size(120, 90)
w.move(x, y)
w.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(220 / 255, 30 / 255, 30 / 255, 1))
w.show_all()
Gtk.main()
"""


@gnome
def test_x11_drag_icon_window_is_left_out(cap_session, shell, client):
    """An X11 app's drag icon is a DND-type window, not in the feedback group: it is
    left out as well, and it is back on screen right after the capture."""
    from tests.linux.session import gi_python
    s = cap_session
    if not s.display or not gi_python():
        pytest.skip("needs Xwayland and python3-gi")
    inject.move(shell, 40, 860)
    rect = (980, 670, 240, 180)                        # wallpaper only, below the probe
    before = client.capture(*rect)
    proc = s.popen([gi_python(), "-I", "-c", X11_DND_ICON, "1040", "715"],
                   env={"GDK_BACKEND": "x11"})
    try:
        win = shell.wait_for_window(window_type="dnd", timeout=15)
        assert win["client"] == "x11", win
        time.sleep(0.5)                                # mapped and painted
        during = client.capture(*rect)
        helper = _helper_area(s, shell, *rect, "x11-dnd-helper.png")
    finally:
        proc.terminate()
        proc.wait(10)
    print(f"\nX11 DND window: red pixels helper {_red_pixels(helper)}, "
          f"capture {_red_pixels(during)}; capture vs no-icon {_diff(during, before):.3f}")
    assert _red_pixels(helper) > 5000
    assert _red_pixels(during) == 0 and _diff(during, before) < 3


def _bench_capture(client, rect, n=40):
    """capture() times in ms (sorted) and the median PNG size in bytes."""
    sizes = []
    real_call = lx._call

    def call(*a, **kw):
        body = real_call(*a, **kw)
        if body:
            sizes.append(sum(map(len, body[4])))
        return body

    client.capture(*rect)                               # connected and warm
    times = []
    lx._call = call
    try:
        for _ in range(n):
            t = time.perf_counter()
            im = client.capture(*rect)
            times.append((time.perf_counter() - t) * 1000)
            assert im is not None and im.size == rect[2:]
    finally:
        lx._call = real_call
    return sorted(times), statistics.median(sizes)


@gnome
def test_latency_and_png_size(cap_session, shell, client, probe):
    """End-to-end capture() of 400x400 in Python (target < 40 ms) and the PNG size. The
    time is mostly the shell's PNG encoding (four strips in parallel), so it depends on
    the picture: measured over half probe / half wallpaper and over the wallpaper only
    (a photo compresses worst)."""
    inject.move(shell, 40, 860)
    fx, fy, fw, fh = probe.frame()
    results = {}
    for name, rect in (("probe+wallpaper", (fx - 200, fy, 400, 400)),
                       ("wallpaper", (300, 200, 400, 400))):
        times, png = _bench_capture(client, rect)
        t = time.perf_counter()
        lx._call("CaptureArea", "iiii", *rect, timeout=lx.CAPTURE_TIMEOUT)
        bare = (time.perf_counter() - t) * 1000
        med, p90 = statistics.median(times), times[len(times) * 9 // 10]
        results[name] = med
        print(f"\ncapture() 400x400 over {name}, {len(times)} calls: median {med:.1f} ms, "
              f"p90 {p90:.1f} ms, max {times[-1]:.1f} ms; bare D-Bus call {bare:.1f} ms; "
              f"PNG {png / 1024:.0f} KiB")
    assert all(m < 40 for m in results.values()), results


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(cap_session, shell):
    assert _errors_about(cap_session.log_path.read_text(errors="replace")) == []
