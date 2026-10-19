"""Glass windows and toasts show what is really behind them, and refresh it (#40), in the
isolated GNOME session at scale 1 and 2.

The app's own classes run in one process with the real extension (tests/linux/
glass_app.py): `tools.CropWindow(...).present()` (what DragShiftApp.open_window does) and
`toasts.ToastStack` with a real PNG → JPG conversion. Every capture a LiquidBacking makes
is recorded there, so each backdrop is compared pixel by pixel with the helper's
screenshot of the same screen rect taken before any DragShift window was there.

- The Crop window's backdrop comes from where Mutter really put it (Qt's frameGeometry()
  is (0, 0) under Wayland: the bug from #28), without the window itself.
- The window is focused after present().
- Dragging its title bar 200 px with injected input logs `liquid: refreshed` and gives a
  new backdrop of the new place.
- Partly off-screen after a move: the capture is clamped and padded.
- A toast from a conversion has the backdrop of its slot; after the card under it closes
  and it slides down, it is refreshed there with DragShift's windows left out.
- The cost of a refresh (frame poll, gc, capture, render) is measured and printed.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots and side-by-sides are written there."""
import os
import time
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from dragshift import toasts
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import NAUTILUS_RECT, Files
from tests.linux.glass_app import GlassApp
from tests.linux.probe_window import ProbeWindow
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_capture import _diff, _png
from tests.linux.test_extension import (_errors_about, _has_owner, _state,  # noqa: F401
                                        built_dir, built_zip)

pytestmark = pytest.mark.gnome
SCALES = {1: (1600, 900), 2: (3200, 1800)}     # both 1600x900 logical
# Takes the focus before the Crop window does; clear of the Crop window (also after its
# drag), of its off-screen strip (x >= 1525) and of the toasts (y >= 663).
PROBE = (1100, 60, 400, 300)
SAME = 3.0          # mean abs difference per channel (0..255) for "the same pixels"


def _wait(fn, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def _evidence(img: Image.Image, name: str) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        img.save(Path(out) / name)


def _full(env, name: str) -> Image.Image:
    """The helper's screenshot of the whole stage, in logical px."""
    s, shell = env["session"], env["shell"]
    with Image.open(shell.screenshot(s.root / "tmp" / f"{name}.png")) as im:
        im = im.convert("RGB")
    return im if im.size == (1600, 900) else im.resize((1600, 900), Image.BILINEAR)


def _settled_full(env, timeout: float = 15.0) -> Image.Image:
    last, deadline = None, time.monotonic() + timeout
    while True:
        im = _full(env, "settle")
        if last is not None and _diff(im, last) < 0.05:
            return im
        if time.monotonic() > deadline:
            raise AssertionError("the screen never settled")
        last = im
        time.sleep(0.5)


def _behind(before: Image.Image, rect) -> Image.Image:
    """`rect` (x, y, w, h) cut from a logical screenshot: what was there before."""
    x, y, w, h = rect
    return before.crop((x, y, x + w, y + h))


def _std(im: Image.Image) -> float:
    return sum(ImageStat.Stat(im.convert("RGB")).stddev) / 3


def _window(shell, title) -> dict | None:
    return next((w for w in shell.list_windows() if w["title"] == title), None)


def _side_by_side(*ims: Image.Image) -> Image.Image:
    w, h = sum(i.width for i in ims) + 10 * (len(ims) - 1), max(i.height for i in ims)
    out, x = Image.new("RGB", (w, h), "white"), 0
    for im in ims:
        out.paste(im, (x, 0))
        x += im.width + 10
    return out


@pytest.fixture(scope="module", params=list(SCALES), ids=lambda s: f"scale{s:g}")
def env(request, built_dir):  # noqa: F811
    require_session_tools()
    scale = request.param
    with Session(size=SCALES[scale], extensions=[built_dir], helper=True, scale=scale) as s, \
            ShellHelper(s) as shell:
        shell.wait_ready()
        assert _wait(lambda: _has_owner(s), 10), _state(s)
        time.sleep(4.0)                               # the boot banner goes away
        colours = {"Blue.png": (40, 90, 200), "Green.png": (40, 170, 70),
                   "Yellow.png": (240, 200, 40), "Red.png": (200, 50, 40)}
        with Files(s, shell) as files:
            folder = files.make_folder("glass", {n: _png(Image.new("RGB", (160, 120), c))
                                                 for n, c in colours.items()})
            nautilus = files.open_folder(folder, view="icon")
            photo = s.root / "tmp" / "photo.png"
            Image.radial_gradient("L").resize((640, 480)).convert("RGB").save(photo)
            # Files is drawn unfocused in the reference shot: the Crop window takes the
            # focus, which would otherwise change what is behind it. A probe window
            # outside every compared rect has the focus instead.
            probe = ProbeWindow(s, shell, platform="wayland", geometry=PROBE)
            probe.focus()
            inject.move(shell, 40, 860)               # the pointer out of the way
            env = {"session": s, "shell": shell, "scale": scale, "photo": photo,
                   "nautilus": nautilus}
            env["before"] = _settled_full(env)        # no DragShift window anywhere yet
            _evidence(env["before"], f"before-scale{scale}.png")
            try:
                with GlassApp(s) as app:
                    env["app"] = app
                    yield env
            finally:
                probe.close()


def _wait_state(app, title, pred, timeout=8.0):
    st = None

    def ok():
        nonlocal st
        st = app.cmd(cmd="state", title=title)
        return pred(st)
    assert _wait(ok, timeout, 0.1), st
    return st


def _open_crop(env) -> str:
    app = env["app"]
    title = app.cmd(cmd="crop", path=str(env["photo"]))["title"]
    _wait_state(app, title, lambda st: st["image"] and st["captures"] >= 1)
    return title


def test_crop_window_backdrop_is_what_is_behind_it(env):
    s, shell, app, scale = env["session"], env["shell"], env["app"], env["scale"]
    focused_before = [w["title"] for w in shell.list_windows() if w["focused"]]
    t0 = time.monotonic()
    title = env["crop"] = _open_crop(env)
    shown_after = time.monotonic() - t0
    st = app.cmd(cmd="state", title=title)
    win = _window(shell, title)
    frame = win["frame"]
    src, glass_img = app.images(title)
    behind = _behind(env["before"], frame)
    wrong = _behind(env["before"], st["qt_frame"])
    on_screen = _behind(_full(env, "crop-shown"), frame)
    d, d_wrong = _diff(src, behind), _diff(wrong, behind)
    print(f"\nscale {scale:g}: Crop window at {frame} (Qt's frameGeometry {st['qt_frame']}); "
          f"backdrop from {st['last']['rect']} via {st['last']['source']}, "
          f"{st['last']['ms']:.1f} ms; vs the screen behind: {d:.2f} "
          f"(Qt's rect would give {d_wrong:.2f}); rendered backdrop std {_std(glass_img):.1f}; "
          f"glass after {shown_after:.2f} s; focused {win['focused']} (before: {focused_before}), "
          f"active {st['active']}")
    _evidence(_side_by_side(behind, src, glass_img, on_screen),
              f"crop-behind-source-glass-screen-scale{scale}.png")
    _evidence(_full(env, "crop-full"), f"crop-window-scale{scale}.png")

    assert win["client"] == "wayland" and win["pid"] == app.ready["pid"]
    assert st["last"]["rect"] == list(frame) and frame[2:] == tuple(st["size"])
    assert st["last"]["below"] and st["last"]["source"] == "capture"
    assert d < SAME                                   # exactly what is behind the window
    assert _std(glass_img) > 10                       # a refracted picture, not a flat tint
    assert title not in focused_before
    assert win["focused"] and st["active"]            # present() gave it the focus
    assert app.log_lines("liquid: refreshed")


def test_title_bar_drag_refreshes_the_backdrop(env):
    shell, app, scale = env["shell"], env["app"], env["scale"]
    title = env["crop"]
    frame0 = _window(shell, title)["frame"]
    src0, glass0 = app.images(title)
    n0 = len(app.log_lines("liquid: refreshed"))
    captures0 = app.cmd(cmd="state", title=title)["captures"]
    x, y = frame0[0] + 300, frame0[1] + 18            # the title bar, right of the title
    inject.glide(shell, x, y, 0.25)
    inject.button_down(shell)
    time.sleep(0.15)
    inject.glide(shell, x + 200, y, 0.6)
    inject.button_up(shell)
    t_up = time.monotonic()
    inject.move(shell, 40, 860)

    def refreshed():
        st = app.cmd(cmd="state", title=title)
        win = _window(shell, title)
        return st if (st["captures"] > captures0 and win
                      and st["last"]["rect"] == list(win["frame"])
                      and len(app.log_lines("liquid: refreshed")) > n0) else None
    st = _wait(refreshed, 5, 0.05)
    after = time.monotonic() - t_up
    assert st, app.cmd(cmd="state", title=title)
    frame1 = _window(shell, title)["frame"]
    src1, glass1 = app.images(title)
    behind1 = _behind(env["before"], frame1)
    lines = app.log_lines("liquid: refreshed")
    print(f"\nscale {scale:g}: dragged {frame0[:2]} → {frame1[:2]}; refreshed "
          f"{after:.2f} s after the button went up, {st['captures'] - captures0} capture(s); "
          f"new backdrop vs the screen behind {_diff(src1, behind1):.2f}, vs the old backdrop "
          f"{_diff(src1, src0):.2f}; log: {lines[n0:]}")
    _evidence(_side_by_side(src0, src1, glass1), f"drag-old-new-glass-scale{scale}.png")
    _evidence(_full(env, "crop-dragged"), f"crop-dragged-scale{scale}.png")

    assert abs(frame1[0] - frame0[0] - 200) <= 2 and frame1[1] == frame0[1]
    assert st["captures"] - captures0 == 1            # one refresh once the move settled
    assert _diff(src1, behind1) < SAME
    assert _diff(src1, src0) > 10 and _diff(glass1, glass0) > 5   # it changed
    assert lines[-1].startswith(f"liquid: refreshed {frame1[2]}x{frame1[3]} at "
                                f"({frame1[0]}, {frame1[1]}) from capture")


def test_partly_off_screen_after_a_move_is_clamped_and_padded(env):
    shell, app, scale = env["shell"], env["app"], env["scale"]
    title = env["crop"]
    frame0 = _window(shell, title)["frame"]
    captures0 = app.cmd(cmd="state", title=title)["captures"]
    x, y = frame0[0] + 40, frame0[1] + 18
    inject.glide(shell, x, y, 0.25)
    inject.button_down(shell)
    time.sleep(0.15)
    inject.glide(shell, 1590, y, 0.6)                 # Mutter keeps a part on screen
    inject.button_up(shell)
    inject.move(shell, 40, 860)
    st = _wait_state(app, title, lambda st: st["captures"] > captures0 and
                     st["last"]["rect"] == list(_window(shell, title)["frame"]))
    frame = _window(shell, title)["frame"]
    src, glass_img = app.images(title)
    visible = 1600 - frame[0]
    d = _diff(src.crop((0, 0, visible, frame[3])), _behind(env["before"],
                                                          (frame[0], frame[1], visible, frame[3])))
    print(f"\nscale {scale:g}: window at {frame}, {visible} px on screen; backdrop "
          f"{src.size}, on-screen part vs behind {d:.2f}")
    _evidence(_full(env, "crop-off-screen"), f"crop-off-screen-scale{scale}.png")
    assert frame[0] + frame[2] > 1600                 # partly off the screen
    assert src.size == tuple(frame[2:]) and glass_img.size == tuple(frame[2:])
    assert d < SAME
    # padded: the part off the screen repeats the last column on screen
    assert _diff(src.crop((visible, 0, visible + 1, frame[3])),
                 src.crop((visible - 1, 0, visible, frame[3]))) < SAME
    assert app.cmd(cmd="close", title=title)["ok"]
    assert _wait(lambda: _window(shell, title) is None)


def test_toast_from_a_convert_and_its_refresh_after_a_slide(env):
    s, shell, app, scale = env["session"], env["shell"], env["app"], env["scale"]
    before = env["before"]
    a = app.cmd(cmd="toast", src=str(env["photo"]), timeout=60)["title"]
    st_a = _wait_state(app, a, lambda st: st["image"] and st["captures"] >= 1)
    win_a = _wait(lambda: _window(shell, a), 5)
    src_a, glass_a = app.images(a)
    d_a = _diff(src_a, _behind(before, win_a["frame"]))
    time.sleep(0.4)                                   # faded in
    shot = _full(env, "toast")
    print(f"\nscale {scale:g}: toast {a!r} at {win_a['frame']}; backdrop from "
          f"{st_a['last']['rect']} via {st_a['last']['source']}, {st_a['last']['ms']:.1f} ms, "
          f"vs the screen behind {d_a:.2f}; rendered backdrop std {_std(glass_a):.1f}")
    assert (s.root / "tmp" / "photo.jpg").exists()    # the conversion really ran
    assert st_a["last"]["rect"] == list(win_a["frame"])
    assert d_a < SAME and _std(glass_a) > 10

    b = app.cmd(cmd="toast", src=str(env["photo"]), timeout=60)["title"]
    st_b = _wait_state(app, b, lambda st: st["image"] and st["captures"] >= 1)
    win_b = _wait(lambda: _window(shell, b), 5)
    time.sleep(0.4)
    two = _full(env, "toasts-two")
    corner = (win_b["frame"][0] - 40, win_b["frame"][1] - 40, 1600, 900)
    _evidence(shot, f"toast-scale{scale}.png")
    _evidence(two.crop(corner), f"toasts-two-corner-scale{scale}.png")
    _evidence(_side_by_side(_behind(before, win_a["frame"]), src_a, glass_a,
                            _behind(shot, win_a["frame"])),
              f"toast-behind-source-glass-screen-scale{scale}.png")
    captures_b = st_b["captures"]
    n0 = len(app.log_lines("liquid: refreshed"))
    assert app.cmd(cmd="dismiss", title=a)["ok"]      # b slides down into a's slot
    st_b = _wait_state(app, b, lambda st: st["captures"] > captures_b and
                       len(app.log_lines("liquid: refreshed")) > n0)
    win_b = _window(shell, b)
    src_b, glass_b = app.images(b)
    d_b = _diff(src_b, _behind(before, win_b["frame"]))
    print(f"after the slide: {b!r} at {win_b['frame']}, refreshed from {st_b['last']['rect']} "
          f"(own windows left out: {bool(st_b['last']['below'])}), vs the screen behind "
          f"{d_b:.2f}; log: {app.log_lines('liquid: refreshed')[n0:]}")
    assert win_b["frame"] == win_a["frame"]           # in the bottom slot now
    assert st_b["last"]["rect"] == list(win_b["frame"]) and st_b["last"]["below"]
    assert d_b < SAME
    assert win_b["frame"][2:] == (toasts.W, toasts.H)
    app.cmd(cmd="dismiss", title=b)


def test_refresh_cost(env):
    """What one refresh costs: the frame poll (every TRACK_MS while a window is open),
    gc.collect, the capture (both on the GUI thread) and the render (a pool thread), for
    the Crop window (820x680) and a toast (380x104)."""
    shell, app, scale = env["shell"], env["app"], env["scale"]
    crop = _open_crop(env)
    card = app.cmd(cmd="toast", src=str(env["photo"]), keep=True, timeout=60)["title"]
    _wait_state(app, card, lambda st: st["image"] and st["captures"] >= 1)
    rows = {}
    for name, title in (("Crop 820x680", crop), ("toast 380x104", card)):
        rows[name] = app.cmd(cmd="bench", title=title, n=12, timeout=180)
        assert "error" not in rows[name], rows[name]
    print(f"\nscale {scale:g}, medians (max) in ms over 12 runs:")
    for name, r in rows.items():
        print(f"  {name}: poll {r['poll']['median']:.2f} ({r['poll']['max']:.2f}), "
              f"gc {r['gc']['median']:.1f} ({r['gc']['max']:.1f}), "
              f"capture {r['capture']['median']:.1f} ({r['capture']['max']:.1f}), "
              f"render {r['render']['median']:.1f} ({r['render']['max']:.1f}), "
              f"refresh to repaint {r['total']['median']:.1f} ({r['total']['max']:.1f})")
    for r in rows.values():
        assert r["total"]["n"] == 12 and r["poll"]["median"] < 5
        assert r["total"]["median"] < 1500
    app.cmd(cmd="dismiss", title=card)
    app.cmd(cmd="close", title=crop)
    assert _wait(lambda: _window(shell, crop) is None)


def test_no_errors_from_the_extension_in_the_shell_log(env):
    assert _errors_about(env["session"].log_path.read_text(errors="replace")) == []
