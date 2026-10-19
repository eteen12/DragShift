"""The radial menu at the pointer, on top and unfocused, on GNOME Wayland (#34).

DragShift runs as a native Wayland client (#10, docs/linux/display-backend.md), so
`RadialMenu.popup` asks DragShift's shell extension to place its window (PlaceWindow, by
the title "DragShift menu") right before show(). These tests run the real menu in the
isolated session:

- `tests/linux/menu_driver.py`: the real `RadialMenu` in its own process, popup() on
  command; checks the frame against the clamped rect, the stacking above Files after
  Files is activated, the focus, the pointer from the extension, the glass backdrop
  (captured by the extension from the menu's rect) and a second monitor.
- the real app (`python -m dragshift`): its trigger opens the menu at an injected pointer.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots and numbers are written there.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift.radial import MENU_TITLE, SIZE
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.menu_driver import MenuDriver
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_display_backend import (_focused, files, files_window,  # noqa: F401
                                              session, shell)
from tests.linux.test_extension import (_has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
TOLERANCE = 2          # px, frame vs the expected rect
MONITOR = (0, 0, 1600, 900)


def _evidence(name: str, data=None, path: Path | None = None) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if not out:
        return
    if path is not None:
        shutil.copy(path, Path(out) / name)
    else:
        Path(out, name).write_text(json.dumps(data, indent=1))


def expected_tl(x: int, y: int, monitor=MONITOR) -> tuple[int, int]:
    """Where the menu centred on (x, y) must land: kept on the monitor under the pointer,
    exactly as radial.py clamps on Windows (QRect.right() = x + width - 1)."""
    mx, my, mw, mh = monitor
    return (min(max(x - SIZE // 2, mx), mx + mw - 1 - SIZE),
            min(max(y - SIZE // 2, my), my + mh - 1 - SIZE))


def _near(frame, tl) -> bool:
    return (abs(frame[0] - tl[0]) <= TOLERANCE and abs(frame[1] - tl[1]) <= TOLERANCE
            and tuple(frame[2:]) == (SIZE, SIZE))


def _inside(frame, monitor=MONITOR) -> bool:
    mx, my, mw, mh = monitor
    x, y, w, h = frame
    return mx <= x and my <= y and x + w <= mx + mw and y + h <= my + mh


@pytest.fixture
def driver(session, shell):  # noqa: F811
    with MenuDriver(session, shell) as d:
        assert d.ready["platform"] == "wayland"
        yield d


# ------------------------------------------------------------------ placement
@pytest.mark.parametrize("at", [(300, 300), (1590, 890), (800, 10), (5, 450)],
                         ids=["300,300", "1590,890", "top-edge", "left-edge"])
def test_menu_opens_at_the_pointer_clamped_to_the_monitor(session, shell, files_window,  # noqa: F811
                                                          driver, at):
    """The frame is the rect centred on the pointer, moved onto the monitor at its edges,
    ±2 px; it is above the other windows and Files keeps the focus. The extension placed
    it before it mapped (PlaceWindow by the menu's title, reply False: not mapped yet)."""
    monitor, area = driver.area(*at)
    tl = expected_tl(*at, monitor=area or monitor)
    rows = []
    for _ in range(2):                   # first show and a re-show of the same window
        shown = driver.popup(*at)
        w = driver.window()
        rows.append(dict(at=at, expected=tl, work_area=area, frame=shown["frame"],
                         places=shown["places"], above=w["above"], focused=_focused(shell)))
        if area is None and not _near(shown["frame"], tl):
            pytest.xfail(f"Mutter keeps windows out of the top bar and the dock: frame "
                         f"{shown['frame']} for {tl}. radial.py clamps to the work area once "
                         "#42 adds platform_linux.work_area.")
        assert _near(shown["frame"], tl), rows[-1]
        assert _inside(shown["frame"])
        assert shown["places"] == [[MENU_TITLE, *tl, False]]
        assert shown["title"] == MENU_TITLE and shown["tl"] == list(tl)
        assert w["above"] and not w["focused"]
        assert _focused(shell) == [files_window.window["title"]]
        driver.hide()
    print("\n" + "\n".join(map(str, rows)))
    _evidence(f"placement-{at[0]}-{at[1]}.json", rows)


def test_menu_stays_above_files_and_never_takes_the_focus(session, shell, files_window,  # noqa: F811
                                                          driver):
    """ActivateWindow(Files) while the menu is open: the menu stays above Files in
    ListWindows, is not focused, and Files is."""
    before = shell.screenshot(session.root / "tmp" / "files-alone.png")
    shown = driver.popup(300, 300)
    files_id = files_window.window["id"]
    assert shell.activate_window(files_id)
    time.sleep(0.5)
    listed = {w["id"]: w for w in shell.list_windows()}
    me = driver.window()
    shot = shell.screenshot(session.root / "tmp" / "menu-over-files.png")
    _evidence("menu-over-files.png", path=shot)
    record = dict(frame=me["frame"], menu_stacking=me["stacking"],
                  files_stacking=listed[files_id]["stacking"], above=me["above"],
                  focused=_focused(shell))
    print("\n", record)
    _evidence("stacking.json", record)
    assert me["stacking"] > listed[files_id]["stacking"]
    assert me["above"] and not me["focused"]
    assert _focused(shell) == [files_window.window["title"]]
    assert _near(me["frame"], expected_tl(300, 300))
    # and it is really drawn there, over Files: the menu's square changed, the rest didn't
    x, y = shown["tl"]
    with Image.open(before) as a, Image.open(shot) as b:
        a, b = (np.asarray(i.convert("RGB"), dtype=np.int16) for i in (a, b))
    inside = np.abs(a[y + 150:y + 250, x + 150:x + 250] - b[y + 150:y + 250, x + 150:x + 250])
    outside = np.abs(a[y:y + SIZE, x + SIZE + 20:x + SIZE + 120] -
                     b[y:y + SIZE, x + SIZE + 20:x + SIZE + 120])
    print(f"hub area changed by {inside.mean():.1f}, Files beside it by {outside.mean():.1f}")
    assert inside.mean() > 8 and outside.mean() < 2


def test_menu_backdrop_is_the_extensions_capture_of_its_rect(session, shell,  # noqa: F811
                                                             files_window, driver):
    """The glass is refracted from what is under the menu: the extension's capture
    (glass.LAST_SOURCE == "capture", not the wallpaper) of exactly the clamped rect, and
    its pixels are the screen's there (Files and the top bar), apart from the patch
    under the pointer that popup() erases."""
    for at in ((300, 300), (1590, 890)):
        tl = expected_tl(*at)
        ref = shell.screenshot_area(*tl, SIZE, SIZE, session.root / "tmp" / f"under-{at[0]}.png")
        shown = driver.popup(*at)
        driver.hide()
        assert shown["source"] == "capture"
        assert shown["captures"] == [[*tl, SIZE, SIZE, False]]
        with Image.open(ref) as r, Image.open(shown["backdrop"]) as b:
            r = np.asarray(r.convert("RGB"), dtype=np.int16)
            b = np.asarray(b.convert("RGB"), dtype=np.int16)
        assert r.shape == b.shape == (SIZE, SIZE, 3)
        keep = np.ones((SIZE, SIZE), bool)
        cx, cy = at[0] - tl[0], at[1] - tl[1]          # popup() inpaints around the cursor
        keep[max(0, cy - 70):cy + 70, max(0, cx - 76):cx + 76] = False
        diff = np.abs(r - b).mean(axis=2)[keep]
        print(f"\nbackdrop at {tl}: mean |capture - screen| {diff.mean():.2f}, "
              f"screen std {r[keep].std():.1f}")
        assert diff.mean() < 2 and np.percentile(diff, 99) < 12
        assert r[keep].std() > 10            # there is something to see, not one colour
        _evidence(f"backdrop-{at[0]}-{at[1]}.png", path=Path(shown["backdrop"]))


def test_menu_follows_the_extensions_pointer_and_moves_when_reopened(session, shell,  # noqa: F811
                                                                     files_window, driver):
    """popup() without a position (QCursor is stale on Wayland) opens at the pointer from
    the extension; popup() again while it is open moves the mapped window at once."""
    inject.move(shell, 700, 420)
    shown = driver.popup()
    assert _near(shown["frame"], expected_tl(700, 420)), shown
    again = driver.popup(1000, 500)
    assert again["places"] == [[MENU_TITLE, *expected_tl(1000, 500), True]]  # moved now
    assert _wait(lambda: _near(driver.frame(), expected_tl(1000, 500)), 2), driver.frame()
    assert _focused(shell) == [files_window.window["title"]]
    driver.hide()


# ------------------------------------------------------------------ two monitors
@pytest.fixture(scope="module")
def dual(built_dir):  # noqa: F811
    """A second virtual monitor (1280x720) next to the 1600x900 one."""
    require_session_tools()
    with Session(extensions=[built_dir], helper=True,
                 shell_args=["--virtual-monitor", "1280x720"]) as s, ShellHelper(s) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(s)), _state(s)
        yield s, helper


def test_menu_is_clamped_to_the_monitor_under_the_pointer(dual):
    """Near an edge between monitors the menu stays on the pointer's monitor (it does not
    straddle the two), and on the second monitor it is clamped to that monitor."""
    s, shell = dual
    with MenuDriver(s, shell) as d:
        screens = sorted(d.ready["screens"])
        assert len(screens) == 2, screens
        first, second = screens
        sx, sy, sw, sh = second
        rows = []
        for at in ((first[2] - 10, 450),             # right edge of the first
                   (sx + 10, sy + 300),              # left edge of the second
                   (sx + sw - 10, sy + sh - 10),     # corner of the second
                   (sx + 640, sy + 360)):            # middle of the second
            shown = d.popup(*at)
            mon, area = d.area(*at)
            tl = expected_tl(*at, monitor=area or mon)
            rows.append(dict(at=at, monitor=mon, work_area=area, expected=tl,
                             frame=shown["frame"]))
            assert _near(shown["frame"], tl) and _inside(shown["frame"], mon), rows[-1]
            assert shown["source"] == "capture"
            if at == (sx + sw - 10, sy + sh - 10):
                shot = shell.screenshot(s.root / "tmp" / "dual.png")
                _evidence("dual-monitor.png", path=shot)
            d.hide()
    print("\nscreens", screens, "\n" + "\n".join(map(str, rows)))
    _evidence("dual-monitor.json", dict(screens=screens, rows=rows))


# ------------------------------------------------------------------ the real app
@pytest.fixture
def dragshift_app(session, shell):  # noqa: F811
    """`python -m dragshift --quiet` as a user starts it (welcome already seen)."""
    conf = Path(session.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")
    out = session.root / "tmp" / "dragshift-app.out"
    with open(out, "wb") as f:
        proc = session.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                             env={"PYTHONPATH": str(ROOT)}, stdout=f, stderr=subprocess.STDOUT)
    log = Path(session.env()["XDG_STATE_HOME"]) / "DragShift" / "log.txt"
    try:
        assert _wait(lambda: log.exists() and "platform: wayland" in log.read_text(), 20), \
            out.read_text()
        time.sleep(1.5)      # the app's start-up after the log line: watcher, tray
        yield proc, out
    finally:
        shell.release_all()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _app_menu(shell, pid):
    return next((w for w in shell.list_windows() if w["title"] == MENU_TITLE
                 and w["pid"] == pid), None)


@pytest.mark.parametrize("at, start", [((1590, 890), (1550, 850)), ((1100, 300), (1140, 340))],
                         ids=["corner", "desktop"])
def test_real_app_opens_the_menu_at_the_pointer(session, shell, files_window,  # noqa: F811
                                                dragshift_app, at, start):
    """The app's own trigger (left button held, moved, Shift) opens the menu on the
    clamped rect around the pointer, above the other windows and unfocused, with the
    glass backdrop from the extension's capture; releasing the button closes it."""
    proc, out = dragshift_app
    tl = expected_tl(*at)
    inject.glide(shell, *start, dur=0.2)      # empty desktop, off Files
    inject.button_down(shell)
    try:
        inject.glide(shell, *at, dur=0.3)
        with inject.hold_key(shell, "Shift_L"):
            assert _wait(lambda: _app_menu(shell, proc.pid) is not None, 3), out.read_text()
            time.sleep(0.4)
            w = _app_menu(shell, proc.pid)
            shot = shell.screenshot(session.root / "tmp" / f"app-{at[0]}.png")
            _evidence(f"app-menu-{at[0]}-{at[1]}.png", path=shot)
    finally:
        inject.button_up(shell)
    assert _wait(lambda: _app_menu(shell, proc.pid) is None, 3)       # closed on release
    line = [ln for ln in out.read_text().splitlines() if ln.startswith("menu: at")][-1]
    print("\n", w["frame"], w["above"], w["focused"], "|", line)
    assert _near(w["frame"], tl) and _inside(w["frame"])
    assert w["above"] and not w["focused"]
    assert line.startswith(f"menu: at ({tl[0]}, {tl[1]}) {SIZE}x{SIZE}")
    assert line.endswith("backdrop: capture")
