"""CaptureAreaExcludingCaller (#28) and platform_linux.capture(exclude_self=True): the
screen under a rect without DragShift's own windows, so a visible toast or tool window
can re-capture what is behind it.

The extension knows the caller by its bus connection (the pid the bus daemon reports),
so the windows and the capture run in one process, like in the app: tests/linux/
own_window.py, started inside the isolated session. Over a Files (Nautilus) window,
like a real backdrop.
"""
import io
import statistics
import time
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from PIL import Image

from dragshift import glass, platform_linux as lx
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import NAUTILUS_RECT, Files
from tests.linux.own_window import SOLID, OwnWindows
from tests.linux.probe_window import qt_platform
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_capture import _diff, _evidence, _png, fake  # noqa: F401
from tests.linux.test_extension import (SRC, _errors_about, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.linux.test_pointer import _reset_client

gnome = pytest.mark.gnome

SIZE = (1600, 900)
# Own windows go over the Files window (NAUTILUS_RECT = 90, 150, 760, 560).
OVER = (250, 200, 400, 300)           # over the coloured thumbnails
ASIDE = (1000, 250, 400, 300)           # off the Files window, over the wallpaper


def _solid_pixels(im: Image.Image, tol: int = 40) -> int:
    """Pixels within `tol` of the own windows' SOLID colour, which nothing else shows."""
    px = np.asarray(im.convert("RGB"), dtype=np.int16)
    return int((np.abs(px - SOLID) <= tol).all(axis=2).sum())


# ------------------------------------------------------------------ client, no shell
def test_exclude_self_asks_for_the_callers_windows_to_be_left_out(fake):  # noqa: F811
    im = lx.capture(100, 50, 400, 300, exclude_self=True)
    assert im.size == (400, 300) and _diff(im, fake.screen.crop((100, 50, 500, 350))) == 0
    (method, sig, args, timeout), = fake.calls
    assert (method, sig, args, timeout) == ("CaptureAreaExcludingCaller", "iiii",
                                            (100, 50, 400, 300), lx.CAPTURE_TIMEOUT)
    fake.calls.clear()
    lx.capture(100, 50, 400, 300)
    assert fake.calls[0][0] == "CaptureArea"


def test_exclude_self_is_padded_and_scaled_like_capture_area(fake):  # noqa: F811
    fake.scale = 2
    for rect in ((-60, -40, 200, 150), (1500, 800, 200, 150), (100, 50, 400, 300)):
        assert _diff(lx.capture(*rect, exclude_self=True), lx.capture(*rect)) == 0


def test_an_extension_without_the_method_gives_none_and_grab_screen_the_wallpaper(
        fake, monkeypatch):  # noqa: F811
    """An older extension answers UnknownMethod (a None from _call): grab_screen then
    uses the wallpaper, never Qt's grab with our own window in it."""
    monkeypatch.setattr(lx, "_call", lambda *a, **kw: None)
    assert lx.capture(0, 0, 100, 100, exclude_self=True) is None
    wall = Image.new("RGB", (100, 100), (1, 2, 3))
    monkeypatch.setattr(lx, "wallpaper_crop", lambda *a: wall)
    assert glass.grab_screen(0, 0, 100, 100, below_window=1) is wall
    assert glass.LAST_SOURCE == "wallpaper"


def test_grab_screen_below_our_window_uses_the_excluding_capture(fake):  # noqa: F811
    im = glass.grab_screen(100, 50, 400, 400, below_window=1)
    assert glass.LAST_SOURCE == "capture" and im.size == (400, 400)
    assert fake.calls[0][0] == "CaptureAreaExcludingCaller"


def test_dbus_xml_declares_capture_area_excluding_caller():
    iface = ET.parse(SRC / "dbus.xml").getroot().find("interface")
    want = [("i", "in")] * 4 + [("i", "out")] * 4 + [("aay", "out")]
    for name in ("CaptureArea", "CaptureAreaExcludingCaller"):
        m = iface.find(f"method[@name='{name}']")
        assert [(a.get("type"), a.get("direction")) for a in m.findall("arg")] == want, name
    assert "CaptureAreaExcludingCallerAsync(" in (SRC / "extension.js").read_text()
    # the caller's pid comes from the bus daemon (caller.js, shared with #93's check)
    assert "GetConnectionCredentials" in (SRC / "caller.js").read_text()


# ------------------------------------------------------------------ in the session
@pytest.fixture(scope="module")
def ex_session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(size=SIZE, extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(ex_session):
    with ShellHelper(ex_session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(ex_session)), _state(ex_session)
        yield helper


@pytest.fixture(scope="module")
def nautilus(ex_session, shell):
    """A Files window with a few coloured pictures (none of them SOLID), thumbnails
    loaded, at NAUTILUS_RECT."""
    colours = {"Blue.png": (40, 90, 200), "Green.png": (40, 170, 70),
               "Yellow.png": (240, 200, 40), "Grey.png": (120, 120, 120)}
    with Files(ex_session, shell) as files:
        folder = files.make_folder("own-window-capture",
                                   {n: _png(Image.new("RGB", (160, 120), c))
                                    for n, c in colours.items()})
        files.open_folder(folder, view="icon")
        inject.move(shell, 40, 860)                   # pointer out of the way
        # Placeholder icons can stay still for longer than _settled's 0.5 s before the
        # thumbnailer runs, so wait for every picture's colour too.
        deadline = time.monotonic() + 20
        while not _shows_colours(_settled(ex_session, shell, NAUTILUS_RECT),
                                 colours.values()):
            assert time.monotonic() < deadline, "thumbnails never showed"
            time.sleep(0.5)
        yield files


def _shows_colours(im: Image.Image, colours, tolerance: int = 8) -> bool:
    """Whether every colour in `colours` appears in `im` (within `tolerance`)."""
    seen = [c for _, c in im.convert("RGB").getcolors(maxcolors=1 << 20)]
    return all(any(max(abs(a - b) for a, b in zip(c, want)) <= tolerance for c in seen)
               for want in colours)


@pytest.fixture
def client(ex_session, shell, monkeypatch):
    """platform_linux in the test process (not the windows' owner), on the session's bus."""
    _reset_client()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", ex_session.bus_address)
    try:
        assert _wait(lambda: lx._call("Version") is not None)
        yield lx
    finally:
        _reset_client()


def _helper_area(s, shell, x, y, w, h, name) -> Image.Image:
    path = shell.screenshot_area(x, y, w, h, s.root / "tmp" / name)
    with Image.open(path) as im:
        return im.convert("RGB")


def _settled(s, shell, rect, timeout: float = 15.0) -> Image.Image:
    """The helper's screenshot of `rect` once two in a row 0.5 s apart are the same
    (thumbnails loaded, animations over)."""
    last = None
    deadline = time.monotonic() + timeout
    while True:
        im = _helper_area(s, shell, *rect, "settle.png")
        if last is not None and _diff(im, last) == 0:
            return im
        if time.monotonic() > deadline:
            raise AssertionError(f"{rect} never settled")
        last = im
        time.sleep(0.5)


@gnome
@pytest.mark.parametrize("platform", ["wayland", "xcb"])
def test_own_window_is_left_out(ex_session, shell, nautilus, client, platform):
    """Acceptance: a solid-colour DragShift-style window over Files. Its own capture of
    its rect with exclusion has none of its colour and equals Files without it; without
    exclusion it is all its colour. The helper's screenshot right after the capture still
    shows the window (it was hidden only inside the offscreen paint: no flicker). The same
    method called by another process (this test) does not hide it: the extension goes by
    the caller."""
    s = ex_session
    if platform == "xcb" and qt_platform(s) != "xcb":
        pytest.skip("Qt cannot load xcb here (libxcb-cursor0 missing)")
    with OwnWindows(s, shell, platform=platform) as own:
        own.move(0, *ASIDE)
        behind = _settled(s, shell, OVER)             # Files, before the window covers it
        assert _solid_pixels(behind) == 0
        own.move(0, *OVER)
        shown = _helper_area(s, shell, *OVER, "own-shown.png")
        assert _solid_pixels(shown) > 0.95 * OVER[2] * OVER[3]

        excluded, r = own.capture(*OVER, exclude=True)
        after = _helper_area(s, shell, *OVER, "own-after.png")
        included, _ = own.capture(*OVER, exclude=False)
        other = client.capture(*OVER, exclude_self=True)    # from another process
        win = own.window(0)

        d = _diff(excluded, behind)
        print(f"\nown {win['client']} window over Files: excluded capture {r['ms']:.1f} ms, "
              f"vs Files behind it {d:.3f}; own-colour pixels: excluded "
              f"{_solid_pixels(excluded)}, not excluded {_solid_pixels(included)}, "
              f"helper right after {_solid_pixels(after)}, other caller {_solid_pixels(other)}")
        sbs = Image.new("RGB", (OVER[2] * 4 + 30, OVER[3]), "white")
        for i, im in enumerate((behind, after, excluded, included)):
            sbs.paste(im, (i * (OVER[2] + 10), 0))
        _evidence(sbs, f"own-window-{platform}-side-by-side.png")
        _evidence(shell_full(s, shell, "own-window-full.png"), f"own-window-{platform}-full.png")

        assert win["pid"] == own.pid and win["client"] == ("x11" if platform == "xcb" else platform)
        assert _solid_pixels(excluded) == 0 and d < 3
        assert _solid_pixels(included) > 0.95 * OVER[2] * OVER[3]
        assert _solid_pixels(after) > 0.95 * OVER[2] * OVER[3]
        assert _solid_pixels(other) > 0.95 * OVER[2] * OVER[3]


def shell_full(s, shell, name) -> Image.Image:
    with Image.open(shell.screenshot(s.root / "tmp" / name)) as im:
        return im.convert("RGB")


@gnome
def test_every_window_of_the_caller_is_left_out(ex_session, shell, nautilus):
    """Edge case: the pid matches several windows (menu + toast): all are hidden."""
    s = ex_session
    rect = (200, 190, 560, 400)
    with OwnWindows(s, shell, count=2) as own:
        own.move(0, *ASIDE)
        own.move(1, 1000, 600, 300, 200)
        behind = _settled(s, shell, rect)
        own.move(0, 220, 200, 300, 200)
        own.move(1, 430, 350, 300, 200)               # overlaps window 0
        shown = _helper_area(s, shell, *rect, "two-shown.png")
        excluded, _ = own.capture(*rect, exclude=True)
        print(f"\ntwo own windows: own-colour pixels shown {_solid_pixels(shown)}, "
              f"excluded capture {_solid_pixels(excluded)}; vs behind {_diff(excluded, behind):.3f}")
        assert _solid_pixels(shown) > 2 * 300 * 200 * 0.8
        assert _solid_pixels(excluded) == 0 and _diff(excluded, behind) < 3


@gnome
def test_minimised_own_window_stays_minimised(ex_session, shell, nautilus):
    """Edge case: an own window that is already hidden (minimised) is left alone: it is
    not shown by the capture and is still minimised afterwards."""
    s = ex_session
    with OwnWindows(s, shell, kind="plain", count=2) as own:
        own.move(0, *OVER)
        own.move(1, *ASIDE)
        assert own.cmd(cmd="minimize", index=1)["ok"]
        assert _wait(lambda: own.window(1)["minimized"])
        time.sleep(0.5)                               # the minimise animation is over
        before = _helper_area(s, shell, *ASIDE, "min-before.png")
        excluded, _ = own.capture(*ASIDE, exclude=True)
        after = _helper_area(s, shell, *ASIDE, "min-after.png")
        assert own.window(1)["minimized"] and not own.window(0)["minimized"]
        assert _solid_pixels(before) == _solid_pixels(after) == _solid_pixels(excluded) == 0
        assert _diff(excluded, before) < 3
        # and the visible one came back
        assert _solid_pixels(_helper_area(s, shell, *OVER, "min-own.png")) > \
            0.95 * OVER[2] * OVER[3]


@gnome
def test_real_glass_window_backdrop(ex_session, shell, nautilus):
    """A real dragshift.window.GlassWindow over Files: glass.grab_screen with
    below_window set (what LiquidBacking does for a visible window) is the screen
    capture of Files without the window; with below_window 0 the window is in it."""
    s = ex_session
    rect = (200, 190, 420, 320)
    with OwnWindows(s, shell, kind="glass") as own:
        own.move(0, 1000, 250, 420, 320)
        behind = _settled(s, shell, rect)
        own.move(0, *rect)
        shown = _settled(s, shell, rect)
        excluded, r = own.capture(*rect, exclude=True, via="grab_screen")
        included, _ = own.capture(*rect, exclude=False, via="grab_screen")
        d_ex, d_in = _diff(excluded, behind), _diff(included, behind)
        print(f"\nGlassWindow over Files: grab_screen(below_window=1) source {r['source']}, "
              f"{r['ms']:.1f} ms, vs Files {d_ex:.3f}; below_window=0 vs Files {d_in:.3f}; "
              f"window on screen vs Files {_diff(shown, behind):.3f}")
        sbs = Image.new("RGB", (rect[2] * 3 + 20, rect[3]), "white")
        for i, im in enumerate((shown, excluded, included)):
            sbs.paste(im, (i * (rect[2] + 10), 0))
        _evidence(sbs, "glass-window-side-by-side.png")
        assert r["source"] == "capture"
        assert d_ex < 3 and d_in > 15 and _diff(shown, behind) > 15


@gnome
def test_latency(ex_session, shell, nautilus):
    """400x400 capture() from the windows' process, with and without exclusion (the
    extra cost is the caller's pid lookup on the bus and the hide/show)."""
    s = ex_session
    rect = (300, 250, 400, 400)
    with OwnWindows(s, shell, count=2) as own:
        own.move(0, 350, 300, 300, 200)
        own.move(1, 1000, 250, 300, 200)
        res = {}
        for exclude in (False, True, False, True):
            reply = own.cmd(cmd="bench", rect=list(rect), exclude=exclude, n=40, timeout=60)
            assert reply["ok"]
            res.setdefault(exclude, []).extend(reply["ms"])
        for exclude, times in res.items():
            times.sort()
            print(f"\ncapture() 400x400 over Files, exclude_self={exclude}, {len(times)} calls: "
                  f"median {statistics.median(times):.1f} ms, "
                  f"p90 {times[len(times) * 9 // 10]:.1f} ms, max {times[-1]:.1f} ms")
        assert statistics.median(res[True]) < 40


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(ex_session, shell):
    assert _errors_about(ex_session.log_path.read_text(errors="replace")) == []
