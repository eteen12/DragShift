"""The drag trigger on Linux (#31) in the isolated GNOME session: the real app
(`python -m dragshift`) with DragShift's extension, and real Shift+drags of a file from
Files and from the desktop, injected through the test-helper extension.

- The app starts while the extension is disabled and picks it up once it is enabled,
  without a restart (the watcher's probe timer).
- Shift pressed after moving, and Shift held before the button, open the menu within
  OPEN_WITHIN of the trigger (Shift, or the LINUX_DRAG_THRESHOLD move); a Shift tap over the menu flips it to the Tools ring
  (logged as `ring: Tools`), and a drop beside the petals closes it.
- A drag without Shift, and a Shift+drag with "Enabled" off in the tray, open nothing.
- Every drag leaves the source file as it was.

The menu is placed by #34: its frame must be where the app asked, centred on the
trigger point unless clamped to the work area. The drags then move into the menu window
beside the petals. With DRAGSHIFT_EVIDENCE_DIR set, screenshots go there."""
import io
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift.app import LINUX_APP_ID
from dragshift.radial import MENU_TITLE, SIZE
from dragshift.trigger import LINUX_DRAG_THRESHOLD
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session, gi_python
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (ROOT, UUID, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.linux.test_startup import TRAY_CLICK

pytestmark = pytest.mark.gnome

OPEN_WITHIN = 0.3          # s from the trigger (Shift, or the 20 px move) to the menu window
FOLLOWING = "trigger: following the extension's pointer state"
CORNER = 40                # px into the menu window: inside it, outside every petal
SHIFT_MASK = 1 << 0


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (220, 30, 30)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


PHOTO = _jpeg()


def _evidence(path: Path, name: str) -> None:
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        shutil.copy(path, Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"]) / name)


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
    """DragShift started the way a user starts it, but while the extension is disabled;
    the extension is enabled again a few seconds later."""
    s = session
    assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
    assert _wait(lambda: not _has_owner(s))
    conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")          # no Welcome window
    out = s.root / "tmp" / "dragshift-trigger.out"
    text = lambda: out.read_text(errors="replace")         # noqa: E731
    with open(out, "w") as f:
        proc = s.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                       env={"PYTHONPATH": str(ROOT)}, stdout=f, stderr=subprocess.STDOUT)
    try:
        assert _wait(lambda: "extension not available" in text(), 20), text()
        time.sleep(3.0)
        idle = FOLLOWING not in text()
        assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
        enabled = time.monotonic()
        assert _wait(lambda: _has_owner(s))
        picked = _wait(lambda: FOLLOWING in text(), 20)
        yield {"proc": proc, "text": text, "idle_before": idle,
               "picked_after": time.monotonic() - enabled if picked else None}
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


@pytest.fixture
def source(request, files):
    """Photo.jpg in a Files window or on the desktop: (where, path, icon centre, direction
    to drag in)."""
    if request.param == "nautilus":
        folder = files.make_folder(f"pics-{secrets.token_hex(3)}", {"Photo.jpg": PHOTO})
        files.open_folder(folder)
        path, toward = folder / "Photo.jpg", (1, 0.3)
    else:
        path = files.put_on_desktop({f"Desk-{secrets.token_hex(3)}.jpg": PHOTO})[0]
        toward = (-1, -0.6)
    start = files.icon_center(path.name, request.param)
    before = sorted(p.name for p in path.parent.iterdir())
    yield request.param, path, start, toward
    assert path.read_bytes() == PHOTO, "the dragged file changed"
    assert sorted(p.name for p in path.parent.iterdir()) == before
    if request.param == "desktop":
        # one icon on the desktop per test: DING keeps an icon selected after a drag, and a
        # Shift+press on another one then extends that selection instead of dragging
        path.unlink()


# ------------------------------------------------------------------ helpers
def _menus(shell, app) -> list[dict]:
    return [w for w in shell.list_windows()
            if w["title"] == MENU_TITLE and w["wm_class"] == LINUX_APP_ID
            and w["pid"] == app["proc"].pid and tuple(w["frame"][2:]) == (SIZE, SIZE)]


def _press_and_move(shell, start, toward, shift_first: bool) -> float:
    """Button down on the icon and move the file 2 px a step, slowly over the first 12 px
    (where the toolkit starts its drag), like a hand. Shift goes down before the button
    (shift_first; then the move stops a few steps past the watcher's threshold) or after
    72 px. Returns when the trigger condition became true."""
    x0, y0 = inject.glide(shell, *start, dur=0.25)
    if shift_first:
        shell.key(inject.KEYSYMS["Shift_L"], True)
        time.sleep(0.1)
    shell.button(1, True)
    time.sleep(0.15)
    dx, dy = toward
    crossed, k = None, 0
    while k < 36 and not (shift_first and crossed and time.monotonic() - crossed > 0.03):
        k += 1
        x, y = shell.move_pointer(x0 + dx * 2 * k, y0 + dy * 2 * k)
        if crossed is None and abs(x - x0) + abs(y - y0) >= LINUX_DRAG_THRESHOLD:
            crossed = time.monotonic()
        time.sleep(0.02 if k <= 6 else 0.008)
    if shift_first:
        return crossed
    t = time.monotonic()
    shell.key(inject.KEYSYMS["Shift_L"], True)
    return t


def _wait_menu(shell, app, timeout: float) -> tuple[dict | None, float]:
    deadline = time.monotonic() + timeout
    while True:
        found = _menus(shell, app)
        now = time.monotonic()
        if found or now > deadline:
            return (found[-1] if found else None), now
        time.sleep(0.005)


def _settled_frame(shell, app, timeout: float = 2.0) -> tuple:
    """The menu's frame once Mutter has placed it: the first ListWindows record can still
    hold the provisional position of the new window."""
    deadline = time.monotonic() + timeout
    last, stable = None, 0
    while True:
        menus = _menus(shell, app)
        assert menus, "the menu closed"
        frame = tuple(menus[-1]["frame"])
        stable = stable + 1 if frame == last else 0
        last = frame
        if stable >= 3 or time.monotonic() > deadline:
            return frame
        time.sleep(0.03)


def _cancel(shell) -> None:
    """End a drag without dropping: Escape (Mutter cancels it), then the button and keys."""
    inject.press(shell, "Escape")
    time.sleep(0.1)
    shell.release_all()
    time.sleep(0.3)


def _tray(session, label: str) -> None:
    deadline = time.monotonic() + 15        # the tray item registers shortly after start-up
    while True:
        r = session.run([gi_python(), "-I", "-c", TRAY_CLICK, label])
        if r.returncode == 0 or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    assert r.returncode == 0, r.stdout + r.stderr
    time.sleep(0.5)


def _open_flip_and_drop(session, shell, app, source, shift_first=False, tag=None):
    """A real Shift+drag that opens the menu; then into the menu, a Shift tap (Tools ring)
    and a drop beside the petals (ignored: the menu closes, nothing runs)."""
    where, path, start, toward = source
    mark = len(app["text"]())
    try:
        t = _press_and_move(shell, start, toward, shift_first)
        menu, seen = _wait_menu(shell, app, 3.0)
        assert menu is not None, app["text"]()[mark:]
        latency = seen - t
        frame = _settled_frame(shell, app)
        x, y, _, _ = frame
        px, py, mask = shell.get_pointer()
        print(f"\n{where} shift_first={shift_first}: menu after {latency * 1000:.0f} ms, "
              f"frame {frame} (first listed at {menu['frame']}), pointer ({px}, {py}); "
              f"centre offset ({x + SIZE // 2 - px}, {y + SIZE // 2 - py})")
        assert latency <= OPEN_WITHIN
        log = app["text"]()[mark:]
        at = tuple(map(int, re.search(r"trigger: menu at \((-?\d+), (-?\d+)\)", log).groups()))
        tl = tuple(map(int, re.search(r"menu: at \((-?\d+), (-?\d+)\)", log).groups()))
        assert abs(x - tl[0]) <= 2 and abs(y - tl[1]) <= 2, (frame, tl)   # placed (#34)
        if tl == (at[0] - SIZE // 2, at[1] - SIZE // 2):   # not clamped to the work area
            assert abs(x + SIZE // 2 - at[0]) <= 4 and abs(y + SIZE // 2 - at[1]) <= 4
        if tag:
            _evidence(shell.screenshot(session.root / "tmp" / f"{tag}-shown.png"),
                      f"{tag}-shown.png")
        # into the menu quickly: one that sees no file within NO_DRAG_TIMEOUT ticks of the
        # pointer moving closes itself (a window move, not a file drag)
        inject.glide(shell, x + CORNER, y + CORNER, dur=0.15)
        time.sleep(0.3)
        assert shell.get_pointer()[2] & SHIFT_MASK
        assert _menus(shell, app), "the menu closed before the file entered it"
        if tag:
            _evidence(shell.screenshot(session.root / "tmp" / f"{tag}-open.png"),
                      f"{tag}-open.png")
        shell.key(inject.KEYSYMS["Shift_L"], False)              # tap Shift: other ring
        time.sleep(0.1)
        shell.key(inject.KEYSYMS["Shift_L"], True)
        assert _wait(lambda: "ring: Tools" in app["text"]()[mark:], 2), app["text"]()[mark:]
        time.sleep(0.3)
        if tag:
            _evidence(shell.screenshot(session.root / "tmp" / f"{tag}-tools.png"),
                      f"{tag}-tools.png")
        shell.button(1, False)                                   # drop beside the petals
        time.sleep(0.05)
        shell.key(inject.KEYSYMS["Shift_L"], False)
        assert _wait(lambda: not _menus(shell, app), 2), "the menu stayed open"
    finally:
        shell.release_all()
    time.sleep(0.5)


# ------------------------------------------------------------------ tests
def test_extension_enabled_after_start_is_picked_up(app):
    assert app["idle_before"], "polled before the extension was there"
    assert app["picked_after"] is not None, app["text"]()
    print(f"\nextension picked up {app['picked_after']:.1f} s after it was enabled")
    assert app["picked_after"] < 10


@pytest.mark.parametrize("source", ["nautilus", "desktop"], indirect=True)
def test_shift_after_moving_opens_the_menu(session, shell, app, source):
    _open_flip_and_drop(session, shell, app, source, tag=f"trigger-{source[0]}")


@pytest.mark.parametrize("source", ["nautilus", "desktop"], indirect=True)
def test_shift_before_the_button_opens_the_menu(session, shell, app, source):
    # From the desktop this needs trigger.LINUX_DRAG_THRESHOLD: with a 10 px threshold the
    # menu mapped before DING had started its drag (Shift held at the press), and in 4 of
    # 5 runs of this file no file ever entered the menu.
    _open_flip_and_drop(session, shell, app, source, shift_first=True,
                        tag=f"shift-first-{source[0]}")


@pytest.mark.parametrize("source", ["nautilus"], indirect=True)
def test_drag_without_shift_opens_nothing(session, shell, app, source):
    _, _, start, toward = source
    mark = len(app["text"]())
    try:
        inject.glide(shell, *start, dur=0.25)
        shell.button(1, True)
        time.sleep(0.15)
        inject.glide(shell, start[0] + toward[0] * 150, start[1] + toward[1] * 150, dur=0.5)
        menu, _ = _wait_menu(shell, app, 1.0)
        assert menu is None
    finally:
        _cancel(shell)
    assert "trigger: menu at" not in app["text"]()[mark:]


@pytest.mark.parametrize("source", ["nautilus"], indirect=True)
def test_disabled_in_the_tray_opens_nothing(session, shell, app, source):
    _, _, start, toward = source
    _tray(session, "Enabled")                        # off
    try:
        try:
            _press_and_move(shell, start, toward, shift_first=False)
            menu, _ = _wait_menu(shell, app, 1.0)
            assert menu is None
        finally:
            _cancel(shell)
    finally:
        _tray(session, "Enabled")                    # on again
    _open_flip_and_drop(session, shell, app, source)  # and the trigger works again


@pytest.mark.parametrize("source", ["nautilus"], indirect=True)
def test_extension_stopping_closes_the_open_menu(session, shell, app, source):
    """#1's audit: no pointer state while the menu is open counts as released, so the menu
    never stays up on stale state. Afterwards the trigger works again, no restart."""
    s, (_, _, start, toward) = session, source
    mark = len(app["text"]())
    try:
        _press_and_move(shell, start, toward, shift_first=False)
        menu, _ = _wait_menu(shell, app, 3.0)
        assert menu is not None, app["text"]()[mark:]
        assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
        t = time.monotonic()
        assert _wait(lambda: not _menus(shell, app), 3), "the menu stayed open"
        print(f"\nmenu closed {(time.monotonic() - t) * 1000:.0f} ms after the extension stopped")
        assert "no pointer state from the GNOME Shell extension" in app["text"]()[mark:]
    finally:
        _cancel(shell)
        assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
        assert _wait(lambda: _has_owner(s))
    time.sleep(8)        # the client's back-off (5 s at most) and the watcher's probe (2 s)
    _open_flip_and_drop(session, shell, app, source)
