"""Wallpaper fallback for the glass: platform_linux.wallpaper_crop (#27).

The GNOME settings and the monitor layout are faked, and the wallpapers are generated, so
nothing here reads or changes the real desktop's settings. The only real `gsettings` call
uses the in-memory backend, which returns the schema defaults."""
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

RED, GREEN, BLUE, YELLOW = (230, 20, 20), (20, 200, 40), (30, 40, 220), (240, 220, 10)
MONITOR = (0, 0, 1600, 900)


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def lx(monkeypatch):
    """platform_linux with a fake settings reader and one 1600×900 monitor at (0, 0)."""
    from dragshift import platform_linux
    platform_linux._WALL_CACHE.clear()
    gs = {"picture-uri": "", "picture-uri-dark": "", "picture-options": "zoom",
          "primary-color": "#102030", "color-scheme": "default"}
    monitors = [MONITOR]

    def monitor_at(x, y):
        for m in monitors:
            if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
                return m
        return monitors[0]

    monkeypatch.setattr(platform_linux, "_read_settings", lambda: dict(gs))
    monkeypatch.setattr(platform_linux, "_monitor_at", monitor_at)
    platform_linux.fake_settings, platform_linux.fake_monitors = gs, monitors
    yield platform_linux
    platform_linux._WALL_CACHE.clear()
    del platform_linux.fake_settings, platform_linux.fake_monitors


def quadrants(path, size=(1600, 900)):
    """Wallpaper with a different colour in each quadrant."""
    w, h = size
    im = Image.new("RGB", size, RED)
    d = ImageDraw.Draw(im)
    d.rectangle((w // 2, 0, w, h // 2), fill=GREEN)
    d.rectangle((0, h // 2, w // 2, h), fill=BLUE)
    d.rectangle((w // 2, h // 2, w, h), fill=YELLOW)
    im.save(path)
    return path


def mean(im):
    return tuple(int(round(v)) for v in np.asarray(im.convert("RGB"), dtype=float).reshape(-1, 3).mean(0))


def close(a, b, tol=6):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def use(lx, path, **extra):
    lx.fake_settings.update({"picture-uri": f"file://{path}", **extra})


# ---------------------------------------------------------------- geometry
def test_corner_rects_map_to_corners(lx, tmp_path):
    use(lx, quadrants(tmp_path / "wall.png"))
    for (x, y), colour in (((0, 0), RED), ((1550, 0), GREEN), ((0, 850), BLUE), ((1550, 850), YELLOW)):
        im = lx.wallpaper_crop(x, y, 50, 50)
        assert im.size == (50, 50) and im.mode == "RGB"
        assert close(mean(im), colour), ((x, y), mean(im))


def test_whole_monitor_is_the_wallpaper(lx, tmp_path):
    path = quadrants(tmp_path / "wall.png")
    use(lx, path)
    got = np.asarray(lx.wallpaper_crop(0, 0, 1600, 900), dtype=int)
    want = np.asarray(Image.open(path).convert("RGB"), dtype=int)
    assert np.abs(got - want).mean() < 1


def test_4_3_wallpaper_on_16_9_monitor_is_cropped_not_squashed(lx, tmp_path):
    # 1200×900 picture: red band on top, blue band at the bottom, a white square in the middle.
    # Zoom covers 1600×900 with scale 4/3: 1600×1200, 150 screen px cut off top and bottom,
    # which is the 112.5 source rows of each band.
    im = Image.new("RGB", (1200, 900), GREEN)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1199, 111), fill=RED)
    d.rectangle((0, 789, 1199, 899), fill=BLUE)
    d.rectangle((450, 300, 749, 599), fill=(255, 255, 255))
    im.save(tmp_path / "43.png")
    use(lx, tmp_path / "43.png")
    shot = np.asarray(lx.wallpaper_crop(0, 0, 1600, 900), dtype=int)
    red = (np.abs(shot - RED).sum(2) < 60)
    blue = (np.abs(shot - BLUE).sum(2) < 60)
    assert red.sum() < 1600 * 2 and blue.sum() < 1600 * 2      # bands cropped away (edge rows only)
    white = np.argwhere(shot.min(2) > 200)
    (y0, x0), (y1, x1) = white.min(0), white.max(0)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    assert abs(w - 400) <= 2 and abs(h - 400) <= 2, (w, h)      # a square stays a square, ×4/3
    assert abs((x0 + x1) / 2 - 799.5) <= 1.5 and abs((y0 + y1) / 2 - 449.5) <= 1.5   # centred


def test_portrait_wallpaper_is_cropped_vertically_centred(lx, tmp_path):
    im = Image.new("RGB", (900, 1600), GREEN)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 899, 799), fill=RED)          # top half red, bottom half green
    im.save(tmp_path / "tall.png")
    use(lx, tmp_path / "tall.png")
    # scale 1600/900: only the middle 506 source rows show; the red/green edge is the centre line
    assert close(mean(lx.wallpaper_crop(0, 400, 1600, 40)), RED)
    assert close(mean(lx.wallpaper_crop(0, 460, 1600, 40)), GREEN)


def test_exif_orientation_is_applied(lx, tmp_path):
    # stored as 900×1600, EXIF orientation 6 (rotate 90° clockwise to display) → 1600×900
    stored = Image.open(quadrants(tmp_path / "q.png")).transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6
    stored.save(tmp_path / "phone.jpg", quality=95, exif=exif)
    use(lx, tmp_path / "phone.jpg")
    assert close(mean(lx.wallpaper_crop(100, 100, 50, 50)), RED, tol=12)
    assert close(mean(lx.wallpaper_crop(1450, 750, 50, 50)), YELLOW, tol=12)


def test_second_monitor_uses_its_own_geometry(lx, tmp_path):
    use(lx, quadrants(tmp_path / "wall.png"))
    lx.fake_monitors.append((1600, 0, 800, 600))     # right of the first one, 4:3
    # the 16:9 picture covers the 4:3 monitor: 1067×600, the sides cropped
    assert close(mean(lx.wallpaper_crop(1600, 0, 40, 40)), RED)
    assert close(mean(lx.wallpaper_crop(2360, 560, 40, 40)), YELLOW)
    assert close(mean(lx.wallpaper_crop(1600 + 380, 0, 20, 20)), RED)    # left of the centre line
    assert close(mean(lx.wallpaper_crop(1600 + 400, 0, 20, 20)), GREEN)


def test_rect_off_the_monitor_is_background_colour(lx, tmp_path):
    use(lx, quadrants(tmp_path / "wall.png"))
    im = lx.wallpaper_crop(1500, 800, 400, 400)       # mostly below/right of the monitor
    assert im.size == (400, 400)
    assert close(mean(im.crop((0, 0, 100, 100))), YELLOW)
    assert im.getpixel((399, 399)) == (16, 32, 48)


@pytest.mark.parametrize("options", ["zoom", "scaled", "stretched", "centered", "spanned", "wallpaper"])
def test_every_picture_option_draws_like_zoom(lx, tmp_path, options):
    use(lx, quadrants(tmp_path / "wall.png"), **{"picture-options": options})
    assert close(mean(lx.wallpaper_crop(0, 0, 50, 50)), RED)


# ---------------------------------------------------------------- which picture
def test_dark_style_uses_picture_uri_dark(lx, tmp_path):
    light = quadrants(tmp_path / "light.png")
    Image.new("RGB", (1600, 900), (5, 5, 5)).save(tmp_path / "dark.png")
    use(lx, light, **{"picture-uri-dark": f"file://{tmp_path / 'dark.png'}"})
    assert close(mean(lx.wallpaper_crop(0, 0, 40, 40)), RED)
    lx.fake_settings["color-scheme"] = "prefer-dark"
    assert close(mean(lx.wallpaper_crop(0, 0, 40, 40)), (5, 5, 5))
    lx.fake_settings["picture-uri-dark"] = ""            # no dark picture set: the light one
    assert close(mean(lx.wallpaper_crop(0, 0, 40, 40)), RED)


def test_uri_with_escaped_characters(lx, tmp_path):
    path = quadrants(tmp_path / "my wallpaper é.png")
    lx.fake_settings["picture-uri"] = "file://" + str(path).replace(" ", "%20").replace("é", "%C3%A9")
    assert close(mean(lx.wallpaper_crop(0, 0, 40, 40)), RED)


def test_alpha_is_drawn_over_primary_colour(lx, tmp_path):
    Image.new("RGBA", (1600, 900), (255, 0, 0, 0)).save(tmp_path / "clear.png")
    use(lx, tmp_path / "clear.png", **{"primary-color": "#00ff00"})
    assert lx.wallpaper_crop(0, 0, 10, 10).getpixel((5, 5)) == (0, 255, 0)


# ---------------------------------------------------------------- solid colour fallbacks
def solid(im, colour):
    return im.getcolors() == [(im.width * im.height, colour)]


def test_missing_file_gives_solid_primary_colour(lx, tmp_path):
    use(lx, tmp_path / "gone.png", **{"primary-color": "#3465a4"})
    im = lx.wallpaper_crop(10, 20, 30, 40)
    assert im.size == (30, 40) and solid(im, (0x34, 0x65, 0xA4))


def test_option_none_gives_solid_colour(lx, tmp_path):
    use(lx, quadrants(tmp_path / "wall.png"), **{"picture-options": "none"})
    assert solid(lx.wallpaper_crop(0, 0, 30, 30), (16, 32, 48))


def test_deleted_after_cached_gives_solid_colour(lx, tmp_path):
    path = quadrants(tmp_path / "wall.png")
    use(lx, path)
    assert close(mean(lx.wallpaper_crop(0, 0, 30, 30)), RED)
    path.unlink()
    assert solid(lx.wallpaper_crop(0, 0, 30, 30), (16, 32, 48))


def test_symlink_followed_and_dangling_symlink_is_solid(lx, tmp_path):
    # like ~/.config/background: the picture may be a link to the real file
    target = quadrants(tmp_path / "real.png")
    link = tmp_path / "background"
    link.symlink_to(target)
    use(lx, link)
    assert close(mean(lx.wallpaper_crop(0, 0, 30, 30)), RED)
    target.unlink()
    assert solid(lx.wallpaper_crop(0, 0, 30, 30), (16, 32, 48))


@pytest.mark.parametrize("content", [b"<background><static>...</static></background>", b""])
def test_undecodable_file_gives_solid_colour(lx, tmp_path, content):
    (tmp_path / "slideshow.xml").write_bytes(content)     # e.g. an XML slideshow
    use(lx, tmp_path / "slideshow.xml")
    assert solid(lx.wallpaper_crop(0, 0, 30, 30), (16, 32, 48))


def test_non_file_uri_gives_solid_colour(lx):
    lx.fake_settings["picture-uri"] = "resource:///org/gnome/shell/theme/noise.png"
    assert solid(lx.wallpaper_crop(0, 0, 30, 30), (16, 32, 48))


def test_none_when_settings_or_screen_unknown(lx, monkeypatch, tmp_path):
    use(lx, quadrants(tmp_path / "wall.png"))
    assert lx.wallpaper_crop(0, 0, 0, 10) is None
    monkeypatch.setattr(lx, "_monitor_at", lambda x, y: None)
    assert lx.wallpaper_crop(0, 0, 10, 10) is None
    monkeypatch.setattr(lx, "_read_settings", lambda: None)
    assert lx.wallpaper_crop(0, 0, 10, 10) is None


@pytest.mark.parametrize("text, rgb", [
    ("#000000000000", (0, 0, 0)), ("#ffff80800000", (255, 128, 0)), ("#3465a4", (52, 101, 164)),
    ("#fff", (255, 255, 255)), ("", (0, 0, 0)), ("rgb(1,2,3)", (0, 0, 0))])
def test_parse_color(lx, text, rgb):
    assert lx._parse_color(text) == rgb


# ---------------------------------------------------------------- cache
def test_decoded_once_per_file_version_and_monitor(lx, monkeypatch, tmp_path):
    path = quadrants(tmp_path / "wall.png")
    use(lx, path)
    calls = []
    real = lx._decode_zoom
    monkeypatch.setattr(lx, "_decode_zoom", lambda *a: calls.append(a[1:3]) or real(*a))
    for x in range(0, 1600, 200):
        lx.wallpaper_crop(x, 0, 100, 100)
    assert calls == [(1600, 900)]                     # decoded once, at monitor size
    Image.new("RGB", (1600, 900), (1, 2, 3)).save(path)
    os.utime(path, ns=(1, 1))                         # new file version (different mtime)
    assert lx.wallpaper_crop(0, 0, 10, 10).getpixel((0, 0)) == (1, 2, 3)
    lx.fake_monitors.append((1600, 0, 800, 600))
    lx.wallpaper_crop(1700, 10, 10, 10)
    assert calls == [(1600, 900), (1600, 900), (800, 600)]


# ---------------------------------------------------------------- real settings reader / Qt
FAKE_GSETTINGS = """#!/bin/sh
if [ "$1 $2" = "list-recursively org.gnome.desktop.background" ]; then
  [ -n "$NO_BACKGROUND" ] && { echo 'No such schema' >&2; exit 1; }
  cat <<'EOF'
org.gnome.desktop.background color-shading-type 'solid'
org.gnome.desktop.background picture-options 'zoom'
org.gnome.desktop.background picture-uri 'file:///home/u/.config/background'
org.gnome.desktop.background picture-uri-dark "file:///home/u/it's dark.png"
org.gnome.desktop.background primary-color '#000000000000'
org.gnome.desktop.background show-desktop-icons true
EOF
  exit 0
fi
if [ "$1 $2 $3" = "get org.gnome.desktop.interface color-scheme" ]; then
  [ -n "$NO_SCHEME" ] && { echo 'No such key' >&2; exit 1; }
  echo "'prefer-dark'"; exit 0
fi
exit 1
"""


@pytest.fixture
def fake_gsettings(monkeypatch, tmp_path):
    from dragshift import platform_linux
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gsettings").write_text(FAKE_GSETTINGS)
    (bindir / "gsettings").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return platform_linux


def test_read_settings_parses_gsettings(fake_gsettings, monkeypatch):
    assert fake_gsettings._read_settings() == {
        "picture-options": "zoom", "picture-uri": "file:///home/u/.config/background",
        "picture-uri-dark": "file:///home/u/it's dark.png", "primary-color": "#000000000000",
        "color-scheme": "prefer-dark"}
    monkeypatch.setenv("NO_SCHEME", "1")              # GNOME without the dark-style key
    assert "color-scheme" not in fake_gsettings._read_settings()
    monkeypatch.setenv("NO_BACKGROUND", "1")          # not a GNOME desktop
    assert fake_gsettings._read_settings() is None


def test_read_settings_without_gsettings(monkeypatch, tmp_path):
    from dragshift import platform_linux
    monkeypatch.setenv("PATH", str(tmp_path))
    assert platform_linux._read_settings() is None


@pytest.mark.skipif(shutil.which("gsettings") is None, reason="no gsettings")
def test_read_settings_real_gsettings_defaults(monkeypatch):
    """Read-only, and from the in-memory backend: the schema defaults, not the desktop's."""
    from dragshift import platform_linux
    r = subprocess.run(["gsettings", "list-schemas"], capture_output=True, text=True)
    if "org.gnome.desktop.background" not in r.stdout:
        pytest.skip("no GNOME schemas")
    monkeypatch.setenv("GSETTINGS_BACKEND", "memory")
    s = platform_linux._read_settings()
    assert s["picture-options"] in ("none", "wallpaper", "centered", "scaled", "stretched", "zoom", "spanned")
    assert s["primary-color"].startswith("#") and "picture-uri" in s


def test_monitor_from_qt(qapp):
    from PySide6.QtGui import QGuiApplication
    from dragshift import platform_linux
    g = QGuiApplication.primaryScreen().geometry()
    assert platform_linux._monitor_at(g.x() + 1, g.y() + 1) == (g.x(), g.y(), g.width(), g.height())
    assert platform_linux._monitor_at(-99999, -99999) == (g.x(), g.y(), g.width(), g.height())


def test_grab_screen_uses_wallpaper_without_extension(qapp, monkeypatch, tmp_path):
    """The capture contract (PR #74): no extension capture -> the wallpaper, with or
    without our own windows to leave out; the real Qt monitor lookup is used."""
    from PySide6.QtGui import QGuiApplication
    from dragshift import glass, platform_linux
    g = QGuiApplication.primaryScreen().geometry()
    Image.new("RGB", (g.width(), g.height()), (200, 100, 50)).save(tmp_path / "w.png")
    platform_linux._WALL_CACHE.clear()
    monkeypatch.setattr(platform_linux, "_read_settings", lambda: {
        "picture-uri": f"file://{tmp_path / 'w.png'}", "picture-options": "zoom",
        "primary-color": "#000000"})
    for below in (0, 1):
        im = glass.grab_screen(g.x() + 10, g.y() + 10, 64, 48, below_window=below)
        assert glass.LAST_SOURCE == "wallpaper" and im.size == (64, 48)
        assert close(mean(im), (200, 100, 50))
    platform_linux._WALL_CACHE.clear()
