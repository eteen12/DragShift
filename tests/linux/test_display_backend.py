"""Evidence for the display-backend decision (#10, docs/linux/display-backend.md).

The same menu-sized window (tests/linux/menu_probe.py: DragShift's radial-menu flags) is
shown under each candidate in the isolated session, with Files and the DING desktop
around it, and real Shift+drags from both:

(a) Xwayland, QT_QPA_PLATFORM=xcb (needs libxcb-cursor0; skipped where Qt can't load
    xcb, e.g. this desktop without DRAGSHIFT_XCB_LIBS; the Docker image has it);
(b) native Wayland with one fullscreen transparent overlay (rejected);
(c) native Wayland, placed and kept on top by DragShift's shell extension (PlaceWindow,
    extension/dragshift@eteen12.github.io/placement.js): the decision.

Numbers are printed (pytest -s) and, with DRAGSHIFT_EVIDENCE_DIR set, written there
together with screenshots.
"""
import io
import json
import os
import shutil
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dragshift import platform_linux as lx
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.menu_probe import SIZE, MenuProbe, xcb_loads
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (_has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)

gnome = pytest.mark.gnome

SHIFT_MASK = 1 << 0
# Places to open the menu (its centre), all fully on the 1600x900 monitor; the app clamps
# to the monitor itself (radial.py), so the extension never has to.
POINTS = ((1000, 600), (300, 300), (1390, 690))


def _jpeg(color=(220, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


PHOTO = _jpeg()


def _evidence(name: str, data=None, path: Path | None = None) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if not out:
        return
    if path is not None:
        shutil.copy(path, Path(out) / name)
    else:
        Path(out, name).write_text(json.dumps(data, indent=1))


def _focused(shell) -> list[str]:
    return [w["title"] for w in shell.list_windows() if w["focused"]]


def _tl(x, y):
    return x - SIZE // 2, y - SIZE // 2


# ------------------------------------------------------------------ client, no shell
def test_place_window_client(monkeypatch):
    calls = []
    replies = [(True,), (False,), None]

    def call(method, signature=None, *args, **kw):
        calls.append((method, signature, args))
        return replies.pop(0)

    monkeypatch.setattr(lx, "_call", call)
    assert lx.place_window("DragShift menu", 10.0, 20) is True
    assert lx.place_window("DragShift menu", 10, 20) is False
    assert lx.place_window("DragShift menu", 10, 20) is False     # no extension: no raise
    assert calls[0] == ("PlaceWindow", "sii", ("DragShift menu", 10, 20))


# ------------------------------------------------------------------ the session
@pytest.fixture(scope="module")
def session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(session):
    with ShellHelper(session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(session)), _state(session)
        yield helper


@pytest.fixture
def files(session, shell):
    f = Files(session, shell)
    yield f
    f.close()
    shell.release_all()


@pytest.fixture
def files_window(files):
    """A Files window (focused), and its folder holding Photo.jpg."""
    folder = files.make_folder(f"pics-{time.monotonic_ns()}", {"Photo.jpg": PHOTO})
    win = files.open_folder(folder)
    files.shell.activate_window(win["id"])
    assert _wait(lambda: _focused(files.shell) == [win["title"]], 5), _focused(files.shell)
    return SimpleNamespace(folder=folder, window=win)


def _xcb(session):
    if not xcb_loads(session):
        pytest.skip("Qt xcb plugin does not load here (libxcb-cursor0 missing; "
                    "set DRAGSHIFT_XCB_LIBS or use tests/linux/docker/run.sh)")


# ------------------------------------------------------------------ placement
@gnome
def test_native_wayland_alone_cannot_place_raise_or_unfocus(session, shell, files_window):
    """Why (c) needs the extension: Qt believes move() worked, Mutter put the window
    elsewhere, did not keep it above and gave it the focus (#9, #27 saw the same)."""
    with MenuProbe(session, shell, "wayland") as menu:
        p = menu.show(*POINTS[0])
        w = menu.window()
        print("\nwayland, no extension:", p["qt_frame"], "->", w["frame"], "above", w["above"],
              "focused", w["focused"])
        assert p["qt_frame"] == [*_tl(*POINTS[0]), SIZE, SIZE]
        assert tuple(w["frame"][:2]) != _tl(*POINTS[0])
        assert not w["above"] and w["focused"]
        assert menu.ready["avail"] == menu.ready["screen"]   # no work area under Wayland


@gnome
def test_extension_places_the_menu_exactly_and_keeps_files_focused(session, shell,
                                                                    files_window):
    """(c): PlaceWindow before show() puts the frame on the requested pixel at every
    show, above the other windows, and Files keeps the keyboard focus."""
    rows = []
    with MenuProbe(session, shell, "wayland", place=True) as menu:
        for x, y in POINTS * 2:
            p = menu.show(x, y)
            w = menu.window()
            rows.append(dict(req=_tl(x, y), frame=w["frame"], above=w["above"],
                             focused=_focused(shell), placed_now=p["placed"]))
            assert tuple(w["frame"]) == (*_tl(x, y), SIZE, SIZE)
            assert w["above"] and not w["focused"]
            assert _focused(shell) == [files_window.window["title"]]
            assert p["placed"] is False          # not mapped yet: applied as it maps
            menu.hide()
    print("\n" + "\n".join(map(str, rows)))
    _evidence("placement-wayland-extension.json", rows)


@gnome
def test_extension_moves_a_mapped_window(session, shell, files_window):
    """A toast sliding to its new slot: PlaceWindow on a mapped window moves it now."""
    with MenuProbe(session, shell, "wayland", place=True) as menu:
        menu.show(*POINTS[0])
        assert menu.place(300, 120) is True
        assert _wait(lambda: menu.frame() == (300, 120, SIZE, SIZE), 2), menu.frame()
        assert _focused(shell) == [files_window.window["title"]]


@gnome
def test_extension_moves_only_the_callers_own_windows(session, shell, files_window):
    """Another process's window with the very same title is never moved."""
    with MenuProbe(session, shell, "wayland") as other:
        other.show(*POINTS[1])
        before = other.frame()
        with MenuProbe(session, shell, "wayland", place=True, title=other.title) as menu:
            menu.show(*POINTS[0])
            mine = [w for w in shell.list_windows() if w["title"] == menu.title
                    and w["pid"] == menu.ready["pid"]]
            assert [tuple(w["frame"]) for w in mine] == [(*_tl(*POINTS[0]), SIZE, SIZE)]
            assert menu.place(100, 100) is True
            time.sleep(0.3)
            assert other.frame() == before


@gnome
@pytest.mark.parametrize("platform", ("wayland", "xcb"))
def test_menu_stays_above_files_and_the_desktop(session, shell, files, files_window,
                                                platform):
    """Activating Files, or clicking the desktop (DING), never covers the menu, and the
    menu never takes the focus. (DING's window actor lives in the shell's background
    group, so ListWindows doesn't list it and nothing can sink below it.)"""
    if platform == "xcb":
        _xcb(session)
    files.put_on_desktop({"Photo.jpg": PHOTO})
    files.icon_center("Photo.jpg", "desktop")      # DING is up and showing icons
    x, y = POINTS[0]
    with MenuProbe(session, shell, platform, place=platform == "wayland") as menu:
        menu.show(x, y)
        result = {}
        for name in ("files", "desktop"):
            if name == "files":
                assert shell.activate_window(files_window.window["id"])
            else:
                inject.click(shell, 1400, 150)       # empty desktop, off Files and the menu
            time.sleep(0.5)
            listed = {w["id"]: w for w in shell.list_windows()}
            me = menu.window()
            shot = shell.screenshot(session.root / "tmp" / f"above-{platform}-{name}.png")
            with Image.open(shot) as im:
                centre = im.convert("RGB").getpixel((x, y))
            result[name] = dict(menu_stacking=me["stacking"],
                                files_stacking=listed[files_window.window["id"]]["stacking"],
                                focused=_focused(shell), centre_pixel=centre)
            assert me["stacking"] > listed[files_window.window["id"]]["stacking"]
            assert me["above"] and not me["focused"]
            assert centre == (0, 200, 0)                 # the menu's own green is on top
        assert result["files"]["focused"] == [files_window.window["title"]]
        assert tuple(menu.frame()) == (*_tl(x, y), SIZE, SIZE)
    print("\n", platform, result)
    _evidence(f"stacking-{platform}.json", result)


@gnome
def test_xwayland_places_raises_and_never_focuses_by_itself(session, shell, files_window):
    """(a): under xcb the plain move() lands exactly, the window is a utility window kept
    above (_NET_WM_STATE_ABOVE) that never takes the focus, and Qt sees the GNOME work
    area. No extension needed for any of it."""
    _xcb(session)
    with MenuProbe(session, shell, "xcb") as menu:
        for x, y in POINTS:
            menu.show(x, y)
            w = menu.window()
            assert tuple(w["frame"]) == (*_tl(x, y), SIZE, SIZE)
            assert w["above"] and not w["focused"] and w["client"] == "x11"
            assert w["window_type"] == "utility"
            assert _focused(shell) == [files_window.window["title"]]
            menu.hide()
        assert menu.ready["avail"] != menu.ready["screen"]


# ------------------------------------------------------------------ latency
@gnome
@pytest.mark.parametrize("platform", ("wayland", "xcb"))
def test_menu_open_latency(session, shell, files_window, platform):
    """From the show command to the window being listed by the shell at its place, and
    to Qt's first paint. Both are far below the menu's own 190 ms pop-in animation."""
    if platform == "xcb":
        _xcb(session)
    rows = []
    with MenuProbe(session, shell, platform, place=platform == "wayland") as menu:
        for i in range(12):
            x, y = POINTS[i % len(POINTS)]
            p = menu.show(x, y, settle=0.05)
            rows.append(dict(listed_ms=p["listed_ms"], paint_ms=p["paint_ms"],
                             at_place=menu.frame() == (*_tl(x, y), SIZE, SIZE)))
            menu.hide()
    listed = statistics.median(r["listed_ms"] for r in rows[2:])   # skip the warm-up
    paint = statistics.median(r["paint_ms"] for r in rows[2:])
    print(f"\n{platform}: listed median {listed:.1f} ms, max "
          f"{max(r['listed_ms'] for r in rows[2:]):.1f}; first paint median {paint:.1f} ms")
    _evidence(f"latency-{platform}.json", rows)
    assert all(r["at_place"] for r in rows)
    assert listed < 100


# ------------------------------------------------------------------ drops
@gnome
@pytest.mark.parametrize("platform", ("wayland", "xcb"))
@pytest.mark.parametrize("source", ("files", "desktop"))
def test_shift_drag_into_the_menu(session, shell, files, files_window, platform, source):
    """A real Shift+drag: the menu opens under the pointer mid-drag (as the trigger does),
    the file enters it as text/uri-list and the source file stays.
    - native Wayland (c): Copy and Move are offered and CopyAction is taken;
    - Xwayland (a): with Shift held the bridge offers only MoveAction (#19), the source
      still stays because neither Files nor DING delete on a move to another app."""
    if platform == "xcb":
        _xcb(session)
    if source == "files":
        start = files.icon_center("Photo.jpg", "nautilus")
        path = files_window.folder / "Photo.jpg"
    else:
        files.put_on_desktop({"Desk.jpg": PHOTO})
        start = files.icon_center("Desk.jpg", "desktop")
        path = files.desktop_dir() / "Desk.jpg"
    before = sorted(p.name for p in path.parent.iterdir())
    x, y = 1150, 420
    with MenuProbe(session, shell, platform, place=platform == "wayland") as menu:
        try:
            inject.drag_to(shell, x, y, start=start, hold="Shift_L", dur=0.6, release=False)
            mark = menu.mark()
            menu.show(x, y)
            assert tuple(menu.frame()) == (*_tl(x, y), SIZE, SIZE)
            inject.glide(shell, x + 40, y + 20, dur=0.3)
            enter = menu.wait_for("dragEnter", mark)
            assert shell.get_pointer()[2] & SHIFT_MASK
            inject.button_up(shell)
            drop = menu.wait_for("drop", mark)
        finally:
            shell.release_all()
        time.sleep(0.8)
        focused = _focused(shell)
        shot = shell.screenshot(session.root / "tmp" / f"drop-{platform}-{source}.png")
        _evidence(f"drop-{platform}-{source}.png", path=shot)
        menu_focused = menu.window()["focused"]
    record = dict(enter={k: enter[k] for k in ("urls", "possible", "proposed", "action")},
                  drop={k: drop[k] for k in ("urls", "possible", "proposed", "action")},
                  focused_after=focused)
    print("\n", platform, source, record)
    _evidence(f"drop-{platform}-{source}.json", record)
    assert path.read_bytes() == PHOTO
    assert sorted(p.name for p in path.parent.iterdir()) == before
    assert enter["urls"] == [path.as_uri()]
    assert not menu_focused
    if platform == "wayland":
        assert set(enter["possible"]) == {"CopyAction", "MoveAction"}
        assert drop["action"] == "CopyAction" and drop["urls"] == [path.as_uri()]
    else:
        assert enter["possible"] == ["MoveAction"] and drop["action"] == "MoveAction"


# ------------------------------------------------------------------ backdrop capture
@gnome
@pytest.mark.parametrize("platform", ("wayland", "xcb"))
def test_qt_grab_window_shows_nothing(session, shell, files_window, platform):
    """Audit note on #10: Qt cannot capture the screen on GNOME Wayland either way, so the
    glass backdrop needs the extension's capture or the wallpaper (#26, #27)."""
    if platform == "xcb":
        _xcb(session)
    with MenuProbe(session, shell, platform) as menu:
        g = menu.grab(90, 150, 400, 400)       # the Files window is there
    print("\n", platform, g)
    if platform == "wayland":
        assert g["null"]
    else:
        assert not g["null"] and g["black"] > 0.99


# ------------------------------------------------------------------ (b) the overlay
def _region(shot, box):
    with Image.open(shot) as im:
        data = im.convert("L").crop(box).tobytes()
    return statistics.mean(data), max(data)


@gnome
def test_fullscreen_overlay_blanks_the_shell_and_sinks_below_files(session, shell,
                                                                   files_window):
    """(b), rejected: a fullscreen transparent window covers the monitor, but GNOME treats
    it as a fullscreen app: it hides the top bar and the dock, paints black instead of the
    wallpaper behind it, takes the focus, and drops below Files once Files is used."""
    clock = (720, 4, 880, 28)            # the top bar's clock
    wallpaper = (1250, 60, 1550, 130)    # desktop, away from Files and the menu square
    plain = shell.screenshot(session.root / "tmp" / "overlay-before.png")
    before = {"clock": _region(plain, clock), "wallpaper": _region(plain, wallpaper)}
    with MenuProbe(session, shell, "wayland", overlay=True) as menu:
        menu.show(*POINTS[0], settle=0.8)
        w = menu.window()
        shot = shell.screenshot(session.root / "tmp" / "overlay.png")
        _evidence("overlay.png", path=shot)
        during = {"clock": _region(shot, clock), "wallpaper": _region(shot, wallpaper)}
        assert tuple(w["frame"]) == (0, 0, 1600, 900) and w["focused"]
        shell.activate_window(files_window.window["id"])
        time.sleep(0.5)
        listed = {x["id"]: x for x in shell.list_windows()}
        sunk = menu.window()["stacking"] < listed[files_window.window["id"]]["stacking"]
    print("\noverlay: before", before, "during", during, "sinks below Files", sunk)
    assert before["clock"][1] > 150 and during["clock"][1] < 60      # top bar gone
    assert before["wallpaper"][0] > 25 and during["wallpaper"][0] < 5  # black, no wallpaper
    assert sunk


# ------------------------------------------------------------------ 200 % scale
def _apply_scale(s, scale: float) -> None:
    """The user's real monitor: scale 2 with fractional scaling on
    (scale-monitor-framebuffer). Only the session's own dconf and display config."""
    assert s.run(["gsettings", "set", "org.gnome.mutter", "experimental-features",
                  "['scale-monitor-framebuffer']"]).returncode == 0
    dc = ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
          "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method"]

    def state():
        return s.run(dc + ["org.gnome.Mutter.DisplayConfig.GetCurrentState"]).stdout

    assert _wait(lambda: "'layout-mode': <uint32 1>" in state(), 10), state()
    serial = state().split(",", 1)[0].split()[-1]
    w, h = s.size
    r = s.run(dc + ["org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig", serial, "1",
                    f"[(0, 0, {scale}, 0, true, [('Meta-0', '{w}x{h}@60.000', @a{{sv}} {{}})])]",
                    "@a{sv} {}"])
    assert r.returncode == 0, r.stderr


@pytest.fixture(scope="module")
def hidpi(built_dir):  # noqa: F811
    """A 3200x1800 virtual monitor at scale 2: 1600x900 logical, like 2880x1920 at 2."""
    require_session_tools()
    with Session(size=(3200, 1800), extensions=[built_dir], helper=True) as s, \
            ShellHelper(s) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(s)), _state(s)
        _apply_scale(s, 2.0)
        time.sleep(1.0)
        yield SimpleNamespace(session=s, shell=helper)


def _blurred_pixels(shot: Path, tl) -> int:
    """Pixels between line-black and fill-green along a row through the probe's 1-logical-
    pixel lines: 0 when drawn at the monitor's scale, many when upscaled."""
    with Image.open(shot) as im:
        g = im.convert("L")
        k = g.size[0] // 1600
        y = (tl[1] + 80) * k
        row = [g.getpixel((x, y)) for x in range((tl[0] + 40) * k, (tl[0] + 100) * k)]
    return sum(30 < v < 100 for v in row)


@gnome
@pytest.mark.parametrize("platform", ("wayland", "xcb"))
def test_scale_two(hidpi, platform):
    """At 200 % (the user's monitor) both platforms use logical coordinates (placement
    and GetPointer agree), but only native Wayland draws at device scale; Xwayland
    windows are drawn at 1x and upscaled, i.e. blurry glass and text."""
    s, shell = hidpi.session, hidpi.shell
    if platform == "xcb":
        _xcb(s)
    x, y = 800, 450
    with MenuProbe(s, shell, platform, place=platform == "wayland") as menu:
        menu.show(x, y, settle=0.6)
        frame = menu.frame()
        shot = shell.screenshot(s.root / "tmp" / f"scale2-{platform}.png")
        _evidence(f"scale2-{platform}.png", path=shot)
        blurred = _blurred_pixels(shot, _tl(x, y))
        inject.move(shell, 900, 500)
        pointer = shell.get_pointer()[:2]
        dpr = menu.ready["dpr"]
    print(f"\nscale 2, {platform}: dpr {dpr}, frame {frame}, blurred px {blurred}, "
          f"pointer {pointer}")
    assert frame == (*_tl(x, y), SIZE, SIZE)
    assert tuple(pointer) == (900, 500)
    assert menu.ready["screen"] == [0, 0, 1600, 900]
    if platform == "wayland":
        assert dpr == 2.0 and blurred == 0
    else:
        assert dpr == 1.0 and blurred > 6
