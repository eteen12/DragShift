"""The radial menu's liquid glass on GNOME Wayland (#39), in the isolated session at 100 %
and at the user's 200 %.

`RadialMenu.popup()` takes its backdrop from `glass.grab_screen`: DragShift's extension
captures the menu's rect (the drag icon is never in it), else the wallpaper, else the
painted frost. These tests check what reaches the screen:

- the real menu (`tests/linux/menu_driver.py`) over a Files window full of colourful
  thumbnails and over the bare desktop: the extension's capture of exactly the menu's
  rect, the rendered glass is what is on screen, it is refracted from the backdrop and not
  a flat tint (compared with the frost fallback at the same place);
- the same menu opened during a real drag from Files: the drag icon is on screen but not
  in the capture the glass is made from;
- the real app (`python -m dragshift`): Shift+drags from Files and from the desktop, the
  time from the Shift press to the refracted glass on screen, and its log lines;
- the real app without the extension: click mode falls back to the wallpaper, which was
  decoded at start, so the fallback is fast.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots and numbers are written there.
"""
import json
import os
import re
import shutil
import statistics
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift.radial import R_HUB, R_IN, R_OUT, SIZE, angles
from tests.linux import inject
from tests.linux.files import NAUTILUS_RECT, Files
from tests.linux.menu_driver import MenuDriver
from tests.linux.test_click_mode import App, env, hand_over, menu_window  # noqa: F401
from tests.linux.test_extension import UUID, _has_owner, _wait, built_dir, built_zip  # noqa: F401
from tests.linux.test_menu_place import expected_tl
from tests.linux.test_trigger import _press_and_move

pytestmark = pytest.mark.gnome

OVER_FILES = (NAUTILUS_RECT[0] + NAUTILUS_RECT[2] // 2 + 40, NAUTILUS_RECT[1] + 300)
OVER_DESKTOP = (1150, 450)        # bare wallpaper: right of Files, left of DING's icons
PLACES = {"files": OVER_FILES, "desktop": OVER_DESKTOP}
BUDGET_MS = 40                    # SPEC 4.6: capture -> PNG -> D-Bus -> PIL, per popup
# The capture's median at 200 %: the shell paints and PNG-encodes 800x800 device pixels,
# measured 50-67 ms (#39); a regression guard until the extension encodes at logical size.
HIDPI_CAPTURE_MS = 100
RED = (220, 30, 30)


def _evidence(name: str, data=None, path: Path | None = None) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if not out:
        return
    if path is not None:
        shutil.copy(path, Path(out) / name)
    else:
        Path(out, name).write_text(json.dumps(data, indent=1))


def _png(im: Image.Image) -> bytes:
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _colourful(i: int) -> bytes:
    """A 256x192 picture of hue stripes and a gradient, different for each i."""
    y, x = np.mgrid[0:192, 0:256].astype(np.float32)
    hue = (x / 256 + i / 7 + 0.15 * np.sin(y / 9)) % 1.0
    rgb = np.stack([np.abs(hue * 6 - 3) - 1, 2 - np.abs(hue * 6 - 2),
                    2 - np.abs(hue * 6 - 4)], axis=-1).clip(0, 1)
    rgb *= (0.45 + 0.55 * (y / 192))[..., None]
    return _png(Image.fromarray((rgb * 255).astype(np.uint8)))


def _area(env, x, y, w=SIZE, h=SIZE, name="area.png") -> np.ndarray:  # noqa: F811
    """The helper's screenshot of a logical rect, at logical size (a 200 % shot is halved)."""
    path = env.shell.screenshot_area(x, y, w, h, env.session.root / "tmp" / name)
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.size != (w, h):
            im = im.resize((w, h), Image.BOX)
        return np.asarray(im, dtype=np.float32)


def _rgb(path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.float32)


def _petal_mask(n: int = 4) -> np.ndarray:
    """The petals of an n-petal ring without the hub, the edges and the labels: inside
    each petal by 4 px and 4 degrees, outside a 24 px band around the label radius."""
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32) + 0.5
    dx, dy = x - SIZE / 2, SIZE / 2 - y
    r = np.hypot(dx, dy)
    ang = np.degrees(np.arctan2(dy, dx)) % 360
    inside = np.zeros_like(r, dtype=bool)
    for i in range(n):
        a0, a1 = angles(n, i)
        a = (ang - a0 - 4) % 360
        inside |= a <= (a1 - a0 - 8)
    label = abs(r - (R_IN + R_OUT) / 2) < 24
    return inside & (r > max(R_IN, R_HUB) + 4) & (r < R_OUT - 4) & ~label


def _blur(a: np.ndarray, radius: float) -> np.ndarray:
    from PIL import ImageFilter
    im = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


def _corr(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    la, lb = a[mask].mean(axis=1), b[mask].mean(axis=1)
    return float(np.corrcoef(la, lb)[0, 1])


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def pictures(env):  # noqa: F811
    """A Files window (icon view) on a folder of colourful pictures, thumbnailed."""
    files = Files(env.session, env.shell)
    folder = files.make_folder("colours", {**{f"Colour {i}.png": _colourful(i) for i in range(7)},
                                           "Red.png": _png(Image.new("RGB", (256, 192), RED))})
    win = files.open_folder(folder)
    try:
        yield files, folder, win
    finally:
        files.close()
        env.shell.release_all()


@pytest.fixture
def driver(env):  # noqa: F811
    with MenuDriver(env.session, env.shell) as d:
        yield d


def _menu_lines(text: str) -> dict:
    """The numbers in the menu's log lines (the last of each)."""
    out = {}
    for m in re.finditer(r"menu: linux capture (\d+\.\d) ms", text):
        out["capture_ms"] = float(m[1])
    for m in re.finditer(r"menu: glass on screen (\d+\.\d) ms after popup, first frame "
                         r"(\d+\.\d) ms", text):
        out["glass_ms"], out["first_frame_ms"] = float(m[1]), float(m[2])
    return out


# ------------------------------------------------------------------ the glass on screen
@pytest.mark.parametrize("where", ["files", "desktop"])
def test_glass_is_refracted_from_what_is_behind(env, pictures, driver, where):  # noqa: F811
    """Over Files' thumbnails and over the bare wallpaper: the backdrop is the extension's
    capture of the menu's rect; what is on screen in the petals is the rendered liquid
    glass; it follows what is behind (blurred, refracted), and it is far from the painted
    frost the menu falls back to without a backdrop. 'Not a flat tint': the petals vary
    across the menu like the scene behind them, measured against that frost fallback."""
    at = PLACES[where]
    tl = expected_tl(*at)
    inject.move(env.shell, 40, 860)
    time.sleep(0.3)
    behind = _area(env, *tl, name=f"behind-{where}.png")
    photo = str(pictures[1] / "Colour 0.png")
    mark = len(driver.out.read_text(errors="replace")) if driver.out.exists() else 0
    shown = driver.popup(*at, settle=0.5, paths=[photo])
    on = _area(env, *tl, name=f"glass-{where}.png")
    full = env.shell.screenshot(env.session.root / "tmp" / f"full-{where}.png")
    log = driver.out.read_text(errors="replace")[mark:]
    driver.hide()
    frost_shown = driver.popup(*at, settle=0.5, paths=[photo], frost=True)
    frost = _area(env, *tl, name=f"frost-{where}.png")
    frost_log = driver.out.read_text(errors="replace")
    driver.hide()

    assert shown["source"] == "capture" and shown["captures"] == [[*tl, SIZE, SIZE, False]]
    assert shown["glass"], "the liquid glass was not rendered"
    assert frost_shown["backdrop"] is None and "menu: frost fallback" in frost_log
    mask = _petal_mask(4)
    with Image.open(shown["glass"]) as g:
        glass = np.asarray(g.convert("RGBA"), dtype=np.float32)
    assert glass[..., 3][mask].min() >= 250          # the petals are opaque glass
    backdrop = _rgb(shown["backdrop"])
    rows = dict(
        scale=env.scale, where=where, tl=tl, capture_ms=_menu_lines(log).get("capture_ms"),
        # what is on screen is the rendered glass
        screen_vs_glass=float(np.abs(on - glass[..., :3])[mask].mean()),
        # the glass follows the scene behind it ...
        corr_glass_behind=_corr(on, _blur(behind, 3), mask),
        corr_frost_behind=_corr(frost, _blur(behind, 3), mask),
        # ... but it is not the scene itself: blurred, saturated and bent in the bezel
        glass_vs_behind=float(np.abs(on - behind)[mask].mean()),
        glass_vs_frost=float(np.abs(on - frost)[mask].mean()),
        std_behind=float(behind[mask].std()), std_glass=float(on[mask].std()),
        std_frost=float(frost[mask].std()),
        backdrop_vs_behind=float(np.abs(backdrop - behind).mean()),
    )
    print("\n" + json.dumps(rows))
    tag = f"{where}-scale{env.scale}"
    _evidence(f"glass-{tag}.json", rows)
    _evidence(f"menu-over-{tag}.png", path=full)
    _evidence(f"backdrop-{tag}.png", path=Path(shown["backdrop"]))
    sbs = Image.new("RGB", (SIZE * 3 + 20, SIZE), "white")
    for i, a in enumerate((behind, on, frost)):
        sbs.paste(Image.fromarray(a.astype(np.uint8)), (i * (SIZE + 10), 0))
    out = env.session.root / "tmp" / f"sbs-{tag}.png"
    sbs.save(out)
    _evidence(f"behind-glass-frost-{tag}.png", path=out)

    assert rows["backdrop_vs_behind"] < 3          # the capture is the screen there
    assert rows["screen_vs_glass"] < 6             # the liquid render is what is shown
    assert rows["corr_glass_behind"] > 0.6         # it follows the scene behind ...
    assert rows["glass_vs_frost"] > 8              # ... and is not the frost fallback
    if rows["std_behind"] > 20:                    # something to see (Files, a photo)
        assert rows["std_glass"] > 0.5 * rows["std_behind"], rows


# ------------------------------------------------------------------ the drag icon
def test_drag_icon_is_not_in_the_glass(env, pictures, driver):  # noqa: F811
    """A real drag of a big red picture from Files to the desktop, the menu opened at the
    pointer (as the trigger does): the drag icon is in the helper's screenshot of the
    menu's rect, but the extension's capture the glass is made from equals the screen
    without it (not one more red pixel), and so does the backdrop after the inpaint."""
    files, folder, _ = pictures
    start = files.icon_center("Red.png", "nautilus", folder=folder)
    target = OVER_DESKTOP
    tl = expected_tl(*target)
    inject.glide(env.shell, *target)
    time.sleep(0.3)
    before = _area(env, *tl, name="drag-before.png")
    inject.drag_to(env.shell, *target, start=start, dur=0.8, release=False)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:                # the thumbnail is on screen
            during = _area(env, *tl, name="drag-during.png")
            if _red(during) > _red(before) + 2000:
                break
            time.sleep(0.1)
        shown = driver.popup(*target, settle=0.4)
        full = env.shell.screenshot(env.session.root / "tmp" / "drag-menu.png")
        inject.glide(env.shell, target[0] + 6, target[1] + 6, dur=0.1)   # it enters
        inject.glide(env.shell, *target, dur=0.1)
        time.sleep(0.2)
    finally:
        inject.button_up(env.shell)            # on the hub: refused, nothing moves
        env.shell.release_all()
    time.sleep(1.0)
    driver.hide()
    capture, backdrop = _rgb(shown["capture"]), _rgb(shown["backdrop"])
    rows = dict(scale=env.scale, red_before=_red(before), red_during=_red(during),
                red_in_capture=_red(capture), red_in_backdrop=_red(backdrop),
                capture_vs_before=float(np.abs(capture - before).mean()))
    print("\n" + json.dumps(rows))
    _evidence(f"drag-scale{env.scale}.json", rows)
    _evidence(f"drag-menu-scale{env.scale}.png", path=full)
    _evidence(f"drag-capture-scale{env.scale}.png", path=Path(shown["capture"]))
    assert shown["source"] == "capture"
    assert rows["red_during"] > rows["red_before"] + 2000, "the drag icon never showed"
    assert rows["red_in_capture"] <= rows["red_before"] + 10
    assert rows["red_in_backdrop"] <= rows["red_before"] + 10
    assert rows["capture_vs_before"] < 3
    assert (folder / "Red.png").exists()


def _red(a: np.ndarray) -> int:
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return int(((r > 180) & (g < 80) & (b < 80)).sum())


# ------------------------------------------------------------------ the real app
def _wait_line(app, pattern: str, after: int, timeout: float) -> tuple[re.Match | None, float]:
    """Poll the app's output every millisecond for `pattern` after offset `after`:
    (match, time.monotonic() when it was first seen)."""
    deadline = time.monotonic() + timeout
    while True:
        m = re.search(pattern, app.text()[after:])
        now = time.monotonic()
        if m or now > deadline:
            return m, now
        time.sleep(0.001)


def test_real_app_latency_from_shift_to_glass(env, pictures):  # noqa: F811
    """Shift+drags with the real app, from Files and from the desktop: the time from the
    Shift press to the first frame and to the refracted glass on screen, read from the
    app's own log lines. The capture stays within the spec's 40 ms at 100 % (not at
    200 %, see HIDPI_CAPTURE_MS); the numbers are recorded on #39."""
    files, folder, _ = pictures
    desk = files.put_on_desktop({f"Desk {time.monotonic_ns()}.png": _colourful(3)})[0]
    app = App(env, "--quiet")
    rows = []
    try:
        assert _wait(lambda: "wallpaper: cache warmed" in app.text(), 10), app.text()
        sources = {"files": (lambda: files.icon_center("Colour 2.png", "nautilus",
                                                        folder=folder), (1, 0.3)),
                   "desktop": (lambda: files.icon_center(desk.name, "desktop"), (-1, -0.6))}
        for where, (icon, toward) in sources.items():
            for rep in range(5):
                mark = len(app.text())
                try:
                    t0 = _press_and_move(env.shell, icon(), toward, shift_first=False)
                    m, t_glass = _wait_line(app, r"menu: glass on screen .*", mark, 5)
                    assert m, app.text()[mark:]
                    if rep == 1:
                        time.sleep(0.3)
                        shot = env.shell.screenshot(env.session.root / "tmp" / "app.png")
                        _evidence(f"app-menu-{where}-scale{env.scale}.png", path=shot)
                    frame = menu_window(env)["frame"]
                    hub = (frame[0] + SIZE // 2, frame[1] + SIZE // 2)
                    inject.glide(env.shell, *hub, dur=0.15)        # the file enters
                    time.sleep(0.1)
                    env.shell.button(1, False)          # on the hub: nothing runs
                    time.sleep(0.05)
                finally:
                    env.shell.release_all()
                time.sleep(1.2)                         # the refused icon flies back
                log = app.text()[mark:]
                row = dict(where=where, rep=rep, shift_to_glass_ms=(t_glass - t0) * 1e3,
                           source=re.search(r"backdrop: (\w+)", log)[1],
                           # the file entered before the glass was up: its own ring renders
                           entered_first=-1 < log.find("menu: 1 file(s)") < log.find(
                               "menu: glass on screen"), **_menu_lines(log))
                rows.append(row)
                print(row)
    finally:
        app.stop()
        desk.unlink(missing_ok=True)
        _evidence(f"app-log-scale{env.scale}.txt", path=app.out)
    warm = [r for r in rows if r["rep"] > 0]
    summary = {k: dict(median=statistics.median(r[k] for r in warm),
                       max=max(r[k] for r in warm))
               for k in ("capture_ms", "first_frame_ms", "glass_ms", "shift_to_glass_ms")}
    summary["first_popup"] = rows[0]
    print(f"\nscale {env.scale}: " + json.dumps(summary))
    _evidence(f"latency-scale{env.scale}.json", dict(rows=rows, summary=summary))
    assert all(r["source"] == "capture" for r in rows)
    assert all(r["first_frame_ms"] < r["glass_ms"] for r in rows)   # frost first, then glass
    assert summary["capture_ms"]["median"] < (BUDGET_MS if env.scale == 1 else HIDPI_CAPTURE_MS)


# ------------------------------------------------------------------ no extension
def test_without_the_extension_the_menu_falls_back_to_the_warm_wallpaper(env, pictures):  # noqa: F811
    """With DragShift's extension disabled, a file handed over opens the menu in click
    mode with the wallpaper as its backdrop (`menu: wallpaper fallback`). The app decoded
    the wallpaper at start, so the first fallback takes milliseconds, not the decode."""
    s = env.session
    assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
    try:
        assert _wait(lambda: not _has_owner(s), 10)
        app = App(env, "--quiet")
        try:
            assert _wait(lambda: "wallpaper: cache warmed" in app.text(), 10), app.text()
            warmed = re.search(r"wallpaper: cache warmed for \d+ monitor\(s\) in (\d+) ms",
                               app.text())
            mark = len(app.text())
            hand_over(env, pictures[1] / "Colour 4.png")
            m, _ = _wait_line(app, r"menu: (wallpaper fallback, (\d+\.\d) ms|.*fallback.*)",
                              mark, 10)
            assert m and m[2], app.text()[mark:]
            assert _wait(lambda: menu_window(env) is not None, 5)
            time.sleep(0.6)
            shot = env.shell.screenshot(s.root / "tmp" / "no-extension.png")
            _evidence(f"no-extension-wallpaper-scale{env.scale}.png", path=shot)
            row = dict(scale=env.scale, warm_up_ms=int(warmed[1]),
                       wallpaper_fallback_ms=float(m[2]), line=m[0])
            print("\n" + json.dumps(row))
            _evidence(f"no-extension-scale{env.scale}.json", row)
            assert row["wallpaper_fallback_ms"] < BUDGET_MS
        finally:
            app.stop()
    finally:
        assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
        assert _wait(lambda: _has_owner(s), 10)
