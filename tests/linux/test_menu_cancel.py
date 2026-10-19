"""The drag-mode menu closes on Escape and on a cancelled drag (#32), in the isolated GNOME
session: the real app (`python -m dragshift`) with DragShift's extension, and real
Shift+drags of a file from Files and from the desktop, injected through the test-helper
extension.

Escape during a file drag never reaches DragShift: Mutter takes it and cancels the drag,
and the menu only sees a dragLeave with the button still down (#24). The menu closes
DRAG_CANCEL_GRACE after that when the pointer is still on it.

1. Into the menu, then Escape with the button still held: the menu is gone within
   CLOSE_WITHIN, before the button goes up; nothing is written, the file is unchanged.
2. Into the menu, out again over the source Files window: the menu stays while the file
   is outside (it may come back, as on Windows); releasing the button there closes it and
   the file is unchanged.
3. A Shift+drag that carries no file (a rubber band on the desktop): the menu closes after
   the no-drag timeout, with the button still down.

Every case checks the dragged file's bytes, size and mtime and the folder listing. With
DRAGSHIFT_EVIDENCE_DIR set, screenshots go there."""
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dragshift.radial import SIZE
from dragshift.trigger import DRAG_CANCEL_GRACE, NO_DRAG_TIMEOUT
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import NAUTILUS_RECT, Files
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import ROOT, _has_owner, _state, _wait, built_dir, built_zip  # noqa: F401
from tests.linux.test_trigger import (FOLLOWING, PHOTO, _evidence, _menus, _press_and_move,
                                      _settled_frame, _wait_menu)

pytestmark = pytest.mark.gnome

CLOSE_WITHIN = 0.3         # s from Escape to the menu window gone (#32's acceptance)
BUTTON1 = 1 << 8
CANCELLED = "trigger: drag cancelled over the menu, closing it"
PETAL = (0, -110)          # from the menu's centre: the top petal


# ------------------------------------------------------------------ fixtures
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


@pytest.fixture(scope="module")
def app(session, shell):
    """DragShift started as a user starts it (Welcome already seen)."""
    s = session
    conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")
    out = s.root / "tmp" / "dragshift-cancel.out"
    text = lambda: out.read_text(errors="replace")         # noqa: E731
    with open(out, "w") as f:
        proc = s.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                       env={"PYTHONPATH": str(ROOT)}, stdout=f, stderr=subprocess.STDOUT)
    try:
        assert _wait(lambda: FOLLOWING in text(), 20), text()
        yield {"proc": proc, "text": text}
    finally:
        shell.release_all()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def files(session, shell):
    f = Files(session, shell)
    yield f
    shell.release_all()
    f.close()


def _fingerprint(path: Path):
    st = path.stat()
    return path.read_bytes(), st.st_size, st.st_mtime_ns, sorted(p.name for p in path.parent.iterdir())


@pytest.fixture
def source(request, files):
    """Photo.jpg in a Files window or on the desktop: (where, path, icon centre, direction
    to drag in). Afterwards the file and its folder must be exactly as before."""
    if request.param == "nautilus":
        folder = files.make_folder(f"pics-{secrets.token_hex(3)}", {"Photo.jpg": PHOTO})
        files.open_folder(folder)
        path, toward = folder / "Photo.jpg", (1, 0.3)
    else:
        path = files.put_on_desktop({f"Desk-{secrets.token_hex(3)}.jpg": PHOTO})[0]
        toward = (-1, -0.6)
    start = files.icon_center(path.name, request.param)
    before = _fingerprint(path)
    yield request.param, path, start, toward
    time.sleep(0.5)                       # anything a drop would write lands by now
    assert path.exists(), "the dragged file was moved"
    data, size, mtime, listing = _fingerprint(path)
    assert data == before[0] and size == before[1], "the dragged file changed"
    assert mtime == before[2], "the dragged file was touched"
    assert listing == before[3], f"files were written: {set(listing) ^ set(before[3])}"
    if request.param == "desktop":
        path.unlink()                     # one icon on the desktop per test (#31)


# ------------------------------------------------------------------ helpers
def _open_with_the_file(session, shell, app, source, tag):
    """A real Shift+drag that opens the menu, then the file onto the top petal. Holds
    there past the no-drag timeout, so a menu still open proves the file entered it.
    Returns the menu's frame."""
    where, _, start, toward = source
    mark = len(app["text"]())
    _press_and_move(shell, start, toward, shift_first=False)
    menu, _ = _wait_menu(shell, app, 3.0)
    assert menu is not None, app["text"]()[mark:]
    frame = _settled_frame(shell, app)
    cx, cy = frame[0] + SIZE // 2, frame[1] + SIZE // 2
    inject.glide(shell, cx + PETAL[0], cy + PETAL[1], dur=0.15)
    time.sleep(NO_DRAG_TIMEOUT * 0.015 + 0.4)
    assert _menus(shell, app), "no file entered the menu (the no-drag timeout closed it)"
    _evidence(shell.screenshot(session.root / "tmp" / f"{tag}-open.png"), f"{tag}-open.png")
    return frame


def _wait_gone(shell, app, timeout: float) -> float | None:
    """monotonic() when the menu window was first seen gone, None if it stayed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _menus(shell, app):
            return time.monotonic()
        time.sleep(0.003)
    return None


# ------------------------------------------------------------------ tests
@pytest.mark.parametrize("source", ["nautilus", "desktop"], indirect=True)
def test_escape_over_the_menu_closes_it(session, shell, app, source):
    where = source[0]
    mark = len(app["text"]())
    try:
        _open_with_the_file(session, shell, app, source, f"cancel-esc-{where}")
        pressed = time.monotonic()
        inject.press(shell, "Escape")
        gone = _wait_gone(shell, app, 2.0)
        mask = shell.get_pointer()[2]
        assert gone is not None, "the menu stayed open after Escape\n" + app["text"]()[mark:]
        latency = gone - pressed
        _evidence(shell.screenshot(session.root / "tmp" / f"cancel-esc-{where}-closed.png"),
                  f"cancel-esc-{where}-closed.png")
        print(f"\n{where}: menu gone {latency * 1000:.0f} ms after Escape "
              f"(grace {DRAG_CANCEL_GRACE * 1000:.0f} ms), button still down: "
              f"{bool(mask & BUTTON1)}")
        assert mask & BUTTON1, "closed only because the button went up"
        assert latency <= CLOSE_WITHIN
        assert CANCELLED in app["text"]()[mark:]
    finally:
        shell.release_all()
    time.sleep(0.3)
    assert not _menus(shell, app)


@pytest.mark.parametrize("source", ["nautilus"], indirect=True)
def test_dragged_out_and_released_over_files(session, shell, app, source):
    mark = len(app["text"]())
    try:
        frame = _open_with_the_file(session, shell, app, source, "cancel-out")
        # the point of the Files window farthest from the menu's centre: outside the menu
        nx, ny, nw, nh = NAUTILUS_RECT
        cx, cy = frame[0] + SIZE // 2, frame[1] + SIZE // 2
        tx = nx + 40 if abs(nx + 40 - cx) > abs(nx + nw - 40 - cx) else nx + nw - 40
        ty = ny + 80 if abs(ny + 80 - cy) > abs(ny + nh - 40 - cy) else ny + nh - 40
        assert not (frame[0] <= tx < frame[0] + SIZE and frame[1] <= ty < frame[1] + SIZE)
        inject.glide(shell, tx, ty, dur=0.3)
        time.sleep(DRAG_CANCEL_GRACE + 0.5)
        assert _menus(shell, app), "a drag carried out of the menu closed it"
        _evidence(shell.screenshot(session.root / "tmp" / "cancel-out-outside.png"),
                  "cancel-out-outside.png")
        released = time.monotonic()
        inject.button_up(shell)
        gone = _wait_gone(shell, app, 2.0)
        assert gone is not None, "the menu stayed open after the release"
        print(f"\nreleased over Files at ({tx}, {ty}): menu gone "
              f"{(gone - released) * 1000:.0f} ms after the button went up")
        assert CANCELLED not in app["text"]()[mark:]
    finally:
        shell.release_all()
    time.sleep(0.3)


def test_shift_drag_without_a_file_times_out(session, shell, app):
    """A rubber band on the empty desktop with Shift: the menu opens, no file ever enters,
    and it closes NO_DRAG_TIMEOUT ticks after the pointer moved off its centre."""
    mark = len(app["text"]())
    at = (1100, 300)
    try:
        inject.glide(shell, 1140, 340, dur=0.2)               # empty desktop, off Files
        inject.button_down(shell)
        inject.glide(shell, *at, dur=0.3)
        shell.key(inject.KEYSYMS["Shift_L"], True)
        menu, _ = _wait_menu(shell, app, 3.0)
        assert menu is not None, app["text"]()[mark:]
        moved = time.monotonic()
        inject.glide(shell, at[0] + 40, at[1] + 30, dur=0.1)  # moving, no file
        gone = _wait_gone(shell, app, 3.0)
        mask = shell.get_pointer()[2]
        assert gone is not None, "the menu stayed open without a file"
        print(f"\nno file: menu gone {(gone - moved) * 1000:.0f} ms after the pointer moved "
              f"(timeout {NO_DRAG_TIMEOUT} ticks)")
        assert mask & BUTTON1
        assert 0.5 <= gone - moved <= 2.0
        assert CANCELLED not in app["text"]()[mark:]
    finally:
        shell.release_all()
    time.sleep(0.3)
