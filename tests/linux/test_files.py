"""Files (Nautilus) and DING desktop icons in the isolated session (tests/linux/files.py):
open a folder, put files on the desktop, find a named file's icon by AT-SPI or by the
fallback geometry, and drag it into the probe window for real."""
import io
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.linux import files as files_mod
from tests.linux import inject
from tests.linux.files import NAUTILUS_RECT, Files, FilesError
from tests.linux.probe_window import ProbeWindow, qt_platform
from tests.linux.shell import HelperError

gnome = pytest.mark.gnome

# Where the probe goes for drags: between the Nautilus window (NAUTILUS_RECT) and the
# DING icon column on the right edge.
PROBE_RECT = (900, 200, 520, 440)
COLORS = {"Photo.jpg": (220, 30, 30), "Photo (1).jpg": (30, 60, 220)}


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _contents() -> dict:
    return {**{n: _jpeg(c) for n, c in COLORS.items()}, "notes.txt": "hello\n"}


def _evidence(path: Path, name: str) -> None:
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        shutil.copy(path, Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"]) / name)


def _crop(shot: Path, point, size=32) -> Image.Image:
    x, y = point
    with Image.open(shot) as im:
        return im.convert("RGB").crop((x - size // 2, y - size // 2,
                                       x + size // 2, y + size // 2))


def _dominant(crop: Image.Image):
    """'red' / 'blue' when the centre 12x12 px of the crop is mostly that thumbnail
    colour, else None (no thumbnail, a generic icon)."""
    w, h = crop.size
    raw = crop.crop((w // 2 - 6, h // 2 - 6, w // 2 + 6, h // 2 + 6)).tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    red = sum(r > 150 and g < 90 and b < 90 for r, g, b in px)
    blue = sum(b > 150 and r < 90 and g < 120 for r, g, b in px)
    if max(red, blue) < len(px) * 0.8:
        return None
    return "red" if red > blue else "blue"


def _shows_icon(crop: Image.Image, colour: str) -> bool:
    """The crop is not uniform background: it has detail (a generic icon, an edge) or
    it is the file's own thumbnail colour, which neither Nautilus nor the wallpaper
    has."""
    lo, hi = zip(*crop.getextrema())
    return max(h - l for l, h in zip(lo, hi)) > 40 or _dominant(crop) == colour


@pytest.fixture
def files(gnome_session, shell_helper):
    f = Files(gnome_session, shell_helper)
    yield f
    f.close()
    shell_helper.release_all()


@pytest.fixture
def folder(files):
    return files.make_folder(f"pics-{secrets.token_hex(3)}", _contents())


@pytest.fixture
def desktop(files):
    """Photo.jpg, Photo (1).jpg and notes.txt on the DING desktop (removed afterwards)."""
    paths = files.put_on_desktop(_contents())
    yield files.desktop_dir()
    for p in paths:
        p.unlink(missing_ok=True)


@pytest.fixture
def probe(gnome_session, shell_helper):
    with ProbeWindow(gnome_session, shell_helper) as p:
        p.move(*PROBE_RECT)
        yield p
    shell_helper.release_all()


# ------------------------------------------------------------------ acceptance
@gnome
def test_icon_center_in_nautilus(gnome_session, shell_helper, files, folder):
    """Acceptance: icon_center("Photo.jpg", "nautilus") is inside the window frame and a
    32x32 crop around it is not uniform; exact names only."""
    win = files.open_folder(folder)
    assert win["frame"] == NAUTILUS_RECT and win["client"] == "wayland"
    point = files.icon_center("Photo.jpg", "nautilus")
    assert files.method == "atspi"
    other = files.icon_center("Photo (1).jpg", "nautilus")
    x, y, w, h = NAUTILUS_RECT
    for px, py in (point, other):
        assert x <= px < x + w and y <= py < y + h
    # The grid shows names in collation order: Photo.jpg before Photo (1).jpg.
    assert point != other and point[0] < other[0]
    shot = shell_helper.screenshot(gnome_session.root / "tmp" / "files-nautilus.png")
    crop = _crop(shot, point)
    assert _shows_icon(crop, "red"), crop.getextrema()
    # With thumbnails (the thumbnailer works here), the crop is the file's own colour.
    assert _dominant(crop) in ("red", None)
    assert _dominant(_crop(shot, other)) in ("blue", None)
    _evidence(shot, "nautilus-icons.png")


@gnome
def test_icon_center_on_the_desktop(gnome_session, shell_helper, files, desktop):
    """Acceptance: icon_center("Photo.jpg", "desktop") is inside DING's window (the
    monitor) and the crop around it is not uniform."""
    point = files.icon_center("Photo.jpg", "desktop")
    assert files.method == "atspi"
    other = files.icon_center("Photo (1).jpg", "desktop")
    assert point != other
    w, h = gnome_session.size
    for px, py in (point, other):
        assert 0 <= px < w and 0 <= py < h
    shot = shell_helper.screenshot(gnome_session.root / "tmp" / "files-desktop.png")
    crop = _crop(shot, point)
    assert _shows_icon(crop, "red"), crop.getextrema()
    assert _dominant(crop) in ("red", None)
    assert _dominant(_crop(shot, other)) in ("blue", None)
    _evidence(shot, "desktop-icons.png")


SHIFT_MASK, BUTTON1_MASK = 1 << 0, 1 << 8   # Clutter.ModifierType
PLATFORMS = ("wayland", "xcb")


def _probe_on(gnome_session, shell_helper, platform):
    if platform == "xcb" and qt_platform(gnome_session) != "xcb":
        pytest.skip("Qt xcb plugin does not load here (libxcb-cursor0 missing)")
    probe = ProbeWindow(gnome_session, shell_helper, platform=platform)
    try:
        probe.move(*PROBE_RECT)
    except BaseException:
        probe.close()
        raise
    return probe


def _shift_drag(gnome_session, shell_helper, probe, start, shot_name):
    """Shift+drag from `start` into the probe, as DragShift's trigger does; returns the
    probe's (dragEnter, drop) records."""
    mark = probe.mark()
    target = probe.center()
    inject.drag_to(shell_helper, *target, start=start, hold="Shift_L", dur=0.8,
                   release=False)
    enter = probe.wait_for("dragEnter", after=mark, timeout=10)
    # Shift and the button are both down over the probe, then the button goes up first.
    assert shell_helper.get_pointer()[2] & (SHIFT_MASK | BUTTON1_MASK) == \
        SHIFT_MASK | BUTTON1_MASK
    inject.button_up(shell_helper)
    drop = probe.wait_for("drop", after=mark, timeout=10)
    assert shell_helper.get_pointer()[2] & SHIFT_MASK, "Shift was released before the drop"
    shell_helper.key(inject.KEYSYMS["Shift_L"], False)
    time.sleep(1.5)  # let the probe paint the drop and the source finish the drag
    shot = shell_helper.screenshot(gnome_session.root / "tmp" / shot_name)
    _evidence(shot, shot_name)
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"], shot_name[:-4] + ".jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in (enter, drop)))
    fx, fy, _, _ = probe.frame()  # drag positions are window-local on every platform
    assert (fx + drop["lx"], fy + drop["ly"]) == target
    return enter, drop


def _check_delivery(enter, drop, source: Path, platform: str):
    """The file's URI arrives as text/uri-list. Measured differences (see #19):
    - Native Wayland: the source offers copy and move, the probe answers CopyAction like
      dragshift/radial.py, and enter and drop both carry the URI.
    - Xwayland (xcb): Mutter's DnD bridge offers a single action, and with Shift held
      that is move, so Qt cannot pick copy. The file still stays (checked by the
      caller). From DING, the data read at the drop of such a move is sometimes empty;
      the URI at dragEnter is always there, which is where DragShift reads it.
    Qt never reports Shift in the modifiers: the drop target has no keyboard focus
    during a drag, so DragShift has to read Shift from the shell."""
    uri = source.as_uri()
    assert enter["external"] and "text/uri-list" in enter["formats"], enter
    assert enter["urls"] == [uri] and enter["uri_list"].split() == [uri], enter
    assert drop["accepted"] and "text/uri-list" in drop["formats"], drop
    xwayland_move = platform == "xcb" and drop["possible"] == ["MoveAction"]
    if not (xwayland_move and drop["urls"] == []):
        assert drop["urls"] == [uri] and drop["uri_list"].split() == [uri], drop
    assert drop["action"] == "CopyAction" or xwayland_move, drop


@gnome
@pytest.mark.parametrize("platform", PLATFORMS)
def test_drag_from_nautilus_delivers_the_file_and_keeps_it(
        gnome_session, shell_helper, files, folder, platform):
    """Acceptance: a real Shift+drag of Photo.jpg from Files into the probe delivers
    file://.../Photo.jpg as text/uri-list, and the source stays (copy, not move)."""
    files.open_folder(folder)
    start = files.icon_center("Photo.jpg", "nautilus")
    before = sorted(p.name for p in folder.iterdir())
    with _probe_on(gnome_session, shell_helper, platform) as probe:
        enter, drop = _shift_drag(gnome_session, shell_helper, probe, start,
                                  f"drag-from-nautilus-{platform}.png")
    assert (folder / "Photo.jpg").read_bytes() == _jpeg(COLORS["Photo.jpg"])
    assert sorted(p.name for p in folder.iterdir()) == before
    _check_delivery(enter, drop, folder / "Photo.jpg", platform)


@gnome
@pytest.mark.parametrize("platform", PLATFORMS)
def test_drag_from_the_desktop_delivers_the_file_and_keeps_it(
        gnome_session, shell_helper, files, desktop, platform):
    """Acceptance: the same from a DING desktop icon."""
    start = files.icon_center("Photo.jpg", "desktop")
    before = sorted(p.name for p in desktop.iterdir())
    with _probe_on(gnome_session, shell_helper, platform) as probe:
        enter, drop = _shift_drag(gnome_session, shell_helper, probe, start,
                                  f"drag-from-desktop-{platform}.png")
    assert (desktop / "Photo.jpg").read_bytes() == _jpeg(COLORS["Photo.jpg"])
    assert sorted(p.name for p in desktop.iterdir()) == before
    _check_delivery(enter, drop, desktop / "Photo.jpg", platform)


# ------------------------------------------------------------------ fallback
@gnome
def test_list_view_fallback_matches_atspi(gnome_session, shell_helper, files, folder):
    """The fallback geometry (list view rows) lands on the same icons AT-SPI finds."""
    files.open_folder(folder, view="list")
    order = files.list_order(folder)    # locale-dependent: "notes.txt" first in en_US
    assert sorted(order) == sorted(_contents())
    rows = []
    for name in order:
        files.atspi = None
        exact = files.icon_center(name, "nautilus")
        assert files.method == "atspi"
        rows.append(exact[1])
        files.atspi = False
        guess = files.icon_center(name, "nautilus")
        assert files.method == "fallback"
        assert abs(guess[0] - exact[0]) <= 2 and abs(guess[1] - exact[1]) <= 2, \
            (name, exact, guess)
    assert rows == sorted(rows), "list_order() differs from the rows Nautilus shows"
    shot = shell_helper.screenshot(gnome_session.root / "tmp" / "files-list.png")
    _evidence(shot, "nautilus-list-view.png")


@gnome
def test_desktop_fallback_matches_atspi(files, desktop):
    """DING's stored icon position plus the measured offset lands on the AT-SPI icon."""
    for name in ("notes.txt", "Photo.jpg", "Photo (1).jpg"):
        files.atspi = None
        exact = files.icon_center(name, "desktop")
        files.atspi = False
        guess = files.icon_center(name, "desktop")
        assert abs(guess[0] - exact[0]) <= 2 and abs(guess[1] - exact[1]) <= 2, \
            (name, exact, guess)


@gnome
def test_without_an_accessibility_bus_the_fallback_is_used_and_logged(
        gnome_session, shell_helper, folder, desktop, caplog):
    """Edge case: no AT-SPI bus -> the fallback answers, a warning says so, and the
    point is still on the icon."""
    dead = gnome_session.root / "tmp" / "no-a11y-bus"
    with caplog.at_level(logging.WARNING, logger="tests.linux.files"), \
            Files(gnome_session, shell_helper,
                  bridge_env={"AT_SPI_BUS_ADDRESS": f"unix:path={dead}"}) as f:
        assert not f.atspi_available()
        f.open_folder(folder)
        assert f.views[folder.resolve()] == "list"
        point = f.icon_center("Photo.jpg", "nautilus")
        assert f.method == "fallback"
        on_desktop = f.icon_center("Photo.jpg", "desktop")
        assert f.method == "fallback"
        shot = shell_helper.screenshot()
        for p in (point, on_desktop):
            crop = _crop(shot, p)
            assert _shows_icon(crop, "red") and _dominant(crop) in ("red", None)
    assert "AT-SPI unavailable" in caplog.text and "no accessibility bus" in caplog.text


# ------------------------------------------------------------------ edge cases
@gnome
def test_missing_and_covered_icons_are_reported(gnome_session, shell_helper, files, folder,
                                                desktop, probe):
    files.open_folder(folder)
    with pytest.raises(FilesError, match="no icon for 'Photo'"):
        files.icon_center("Photo", "nautilus", timeout=1)   # a prefix is not a match
    point = files.icon_center("Photo.jpg", "desktop")
    probe.move(point[0] - 100, point[1] - 100, 300, 300)  # now the probe covers it
    with pytest.raises(FilesError, match="covered by"):
        files.icon_center("Photo.jpg", "desktop")


@gnome
def test_move_window(shell_helper, files, folder):
    """MoveWindow puts the frame where asked, also from the maximized state."""
    win = files.open_folder(folder)
    assert shell_helper.move_window(win["id"], 300, 150, 800, 500) == (300, 150, 800, 500)
    assert shell_helper.move_window(win["id"], *NAUTILUS_RECT) == NAUTILUS_RECT
    with pytest.raises(HelperError, match="no window"):
        shell_helper.move_window(1, 0, 0, 10, 10)


# ------------------------------------------------------------------ no session
class FakeNode:
    """A minimal Atspi.Accessible stand-in for the tree walk."""

    def __init__(self, role, name="", box=(0, 0, 0, 0), children=()):
        self.role, self.name, self.box, self.children = role, name, box, list(children)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_extents(self, _coord):
        x, y, w, h = self.box
        return SimpleNamespace(x=x, y=y, width=w, height=h)


FAKE_ATSPI = SimpleNamespace(CoordType=SimpleNamespace(WINDOW=1))


def _grid_cell(name, x):
    return FakeNode("table cell", name, (x, 64, 96, 84), [
        FakeNode("panel", "", (x, 64, 84, 72), [
            FakeNode("panel", "", (x + 18, 70, 48, 48), [
                FakeNode("image", "", (x + 18, 76, 48, 36))]),
            FakeNode("label", name, (x, 124, 84, 18))])])


def test_tree_walk_matches_exact_names_only():
    frame = FakeNode("frame", "pics", children=[
        FakeNode("push button", "Photo.jpg", children=[FakeNode("label", "Photo.jpg")]),
        _grid_cell("Photo.jpg", 316), _grid_cell("Photo (1).jpg", 429)])
    found = files_mod._find_in(frame, "Photo.jpg", "nautilus", FAKE_ATSPI)
    assert [m["icon"] for m in found] == [[334, 76, 48, 36]]
    assert files_mod._find_in(frame, "Photo", "nautilus", FAKE_ATSPI) == []


def test_tree_walk_on_ding_ignores_zero_width_spaces():
    item = FakeNode("filler", "", (1462, 654, 135, 120), [
        FakeNode("panel", "", (1486, 654, 86, 70), [
            FakeNode("filler", "File Photo.jpg", (1486, 656, 86, 68), [
                FakeNode("icon", "", (1488, 658, 82, 64))])]),
        FakeNode("panel", "", (1492, 724, 74, 24), [
            FakeNode("label", "Photo.​jpg", (1496, 726, 66, 18))])])
    frame = FakeNode("frame", "Desktop Icons 1", children=[FakeNode("panel", children=[item])])
    found = files_mod._find_in(frame, "Photo.jpg", "desktop", FAKE_ATSPI)
    assert [m["icon"] for m in found] == [[1488, 658, 82, 64]]
    assert files_mod._find_in(frame, "Photo (1).jpg", "desktop", FAKE_ATSPI) == []
