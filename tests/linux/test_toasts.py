"""Toast placement and stacking (#42) in the isolated GNOME session, at scale 1 and 2.

The real app (`python -m dragshift`) runs with DragShift's extension. The dock is moved
to the bottom, so the work area ends above it. Five quick hand-offs through IPC (what a
second `dragshift` start does) each make an info card; then:
- at most four cards, each at `ToastStack._slot(i)` of the work area (±2 px), above Files,
  which keeps the focus;
- a click on the bottom card's ✕ (real injected input) closes it and the others slide down
  one slot each;
- the card under the pointer outlives its 4.5 s and goes 2.5 s after the pointer leaves.

A driver process with the real ToastStack checks a done card's 6.5 s life, and a session
with a second monitor checks that the cards go to the pointer's monitor.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots are written there."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift import toasts
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import built_dir, built_zip  # noqa: F401  (fixtures)
from tests.linux import inject

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
SCALES = {1: (1600, 900), 2: (3200, 1800)}     # both 1600x900 logical
TITLE = "DragShift toast "
TOL = 2


def _evidence(name: str, img: Image.Image) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        img.save(Path(out) / name)


def _slot(area, i):
    """ToastStack._slot(i) for a work area (x, y, w, h): the frame's (x, y)."""
    x, y, w, h = area
    return (x + w - 1 - toasts.W - toasts.MARGIN,
            y + h - 1 - toasts.MARGIN - toasts.H - i * (toasts.H + toasts.GAP))


def _near(frame, xy) -> bool:
    return abs(frame[0] - xy[0]) <= TOL and abs(frame[1] - xy[1]) <= TOL


def _inside(frame, area) -> bool:
    x, y, w, h = frame
    ax, ay, aw, ah = area
    return ax <= x and ay <= y and x + w <= ax + aw and y + h <= ay + ah


def _toasts(shell) -> list[dict]:
    return [w for w in shell.list_windows() if w["title"].startswith(TITLE)]


def _wait(fn, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def _stack(shell, n, slots):
    """The toast windows, bottom to top, if there are `n` of them, all at `slots`."""
    ws = sorted(_toasts(shell), key=lambda w: -w["frame"][1])
    if len(ws) == n and all(_near(w["frame"], s) for w, s in zip(ws, slots)):
        return ws
    return None


def _settled(shell, n, slots, timeout=5.0):
    """_stack() once it holds, else the toast windows as they are (for the assertions)."""
    return _wait(lambda: _stack(shell, n, slots), timeout) or \
        sorted(_toasts(shell), key=lambda w: -w["frame"][1])


def _dock_to_bottom(s, shell) -> tuple[tuple, tuple]:
    """Ubuntu's dock at the bottom of the screen (the session's own dconf). Returns
    (monitor, work area) once the work area has shrunk for it, else what it is."""
    r = s.run(["gsettings", "set", "org.gnome.shell.extensions.dash-to-dock",
               "dock-position", "'BOTTOM'"])
    print(f"\ndock-position BOTTOM: rc={r.returncode} {r.stderr.strip()}")
    return _wait(lambda: (lambda m, a: (m, a) if a[1] + a[3] < m[1] + m[3] else None)(
        *shell.work_area(800, 450)), 10, 0.2) or shell.work_area(800, 450)


def _start_app(s) -> subprocess.Popen:
    conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "DragShift.conf").write_text("[General]\nwelcomed=true\n")   # no Welcome
    out = open(s.root / "tmp" / "dragshift.out", "w")
    proc = s.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                   env={"PYTHONPATH": str(ROOT)}, stdout=out, stderr=subprocess.STDOUT)
    sock = s.root / "tmp" / ("DragShift-" + (os.environ.get("USER") or "user"))
    assert _wait(sock.exists, 20, 0.1), (s.root / "tmp" / "dragshift.out").read_text()
    return proc


def _handoffs(s, n):
    """`n` quick second starts: each hands {"show": true} to the running app → info card."""
    for _ in range(n):
        r = s.run([sys.executable, "-m", "dragshift"], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                  timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr


def _shot(started, name):
    s, shell, scale = started["session"], started["shell"], started["scale"]
    img = Image.open(shell.screenshot(s.root / "tmp" / f"{name}.png"))
    tag = f"scale{scale:g}"
    _evidence(f"{name}-{tag}.png", img)
    area = started["area"]
    # the bottom-right quarter, where the cards are
    crop = tuple(round(v * scale) for v in (area[0] + area[2] - 520, area[1] + area[3] - 520,
                                            area[0] + area[2], started["monitor"][3]))
    _evidence(f"{name}-corner-{tag}.png", img.crop(crop))
    return img


@pytest.fixture(scope="module", params=list(SCALES), ids=lambda s: f"scale{s:g}")
def started(request, built_dir):  # noqa: F811
    require_session_tools()
    scale = request.param
    with Session(size=SCALES[scale], extensions=[built_dir], helper=True, scale=scale) as s, \
            ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(5.0)   # the shell's boot banner goes away
        monitor, area = _dock_to_bottom(s, shell)
        print(f"scale {scale:g}: monitor {monitor}, work area {area}")
        files = Files(s, shell)
        folder = files.make_folder("pics", {"Photo.jpg": b"x"})
        nautilus = files.open_folder(folder, view="list")
        proc = _start_app(s)
        try:
            yield {"session": s, "shell": shell, "scale": scale, "monitor": monitor,
                   "area": area, "nautilus": nautilus, "proc": proc}
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            files.close()


def test_work_area_is_above_the_dock(started):
    monitor, area = started["monitor"], started["area"]
    assert area[1] >= 32                                   # below the top bar
    assert area[1] + area[3] < monitor[1] + monitor[3], (monitor, area)   # above the dock


def test_five_cards_stack_four_then_slide_and_pause(started):
    s, shell, area = started["session"], started["shell"], started["area"]
    inject.move(shell, 400, 400)            # the pointer on Files, away from the corner
    t0 = time.monotonic()
    _handoffs(s, 5)
    slots = [_slot(area, i) for i in range(4)]
    cards = _settled(shell, 4, slots)
    print(f"5 hand-offs in {time.monotonic() - t0:.2f} s; cards: "
          f"{[(w['title'], w['frame']) for w in cards]}")
    assert len(cards) == 4
    for w, slot in zip(cards, slots):
        assert _near(w["frame"], slot), (w["frame"], slot)
        assert w["frame"][2:] == (toasts.W, toasts.H)
        assert _inside(w["frame"], area), (w["frame"], area)
        assert w["above"] and not w["focused"] and w["client"] == "wayland"
    assert len({w["title"] for w in cards}) == 4
    windows = shell.list_windows()
    focused = [w for w in windows if w["focused"]]
    assert [w["id"] for w in focused] == [started["nautilus"]["id"]]
    order = [w["id"] for w in windows]
    assert all(order.index(w["id"]) > order.index(started["nautilus"]["id"]) for w in cards)
    _shot(started, "toasts-four")

    # ✕ of the bottom card, clicked like a user
    bottom = cards[0]
    x, y = bottom["frame"][:2]
    t_click = time.monotonic()
    inject.move(shell, x + toasts.W - 18, y + 18)    # a jump: gliding over the other
    inject.click(shell)                                # cards would restart their lives
    seen = []

    def slid():
        ws = _toasts(shell)
        seen.extend(w["frame"][1] for w in ws if w["id"] == cards[1]["id"])
        return [w for w in ws if w["id"] == bottom["id"]] == [] and \
            _stack(shell, 3, slots[:3])
    rest = _wait(slid, 3, 0.01)
    print(f"after the click ({time.monotonic() - t_click:.2f} s): "
          f"{[(w['title'], w['frame']) for w in rest]}; card 2's y while sliding: "
          f"{sorted(set(seen))}")
    assert rest and [w["id"] for w in rest] == [c["id"] for c in cards[1:]]
    time.sleep(toasts.SLIDE_MS / 1000 + 0.1)         # the slides have ended
    rest = sorted(_toasts(shell), key=lambda w: -w["frame"][1])
    print(f"after the slides: {[(w['title'], w['frame']) for w in rest]}")
    assert [w["frame"][:2] for w in rest] == slots[:3]       # exactly on the slots
    focused = [w["id"] for w in shell.list_windows() if w["focused"]]
    assert focused == [started["nautilus"]["id"]]          # the click didn't keep the focus
    _shot(started, "toasts-after-close")

    # pointer on the (new) bottom card: it outlives its 4.5 s; the others go
    held = rest[0]
    inject.move(shell, held["frame"][0] + 60, held["frame"][1] + 60)
    gone = _wait(lambda: [w["id"] for w in _toasts(shell)] == [held["id"]], 8, 0.1)
    elapsed = time.monotonic() - t0
    print(f"{elapsed:.1f} s after the hand-offs only the hovered card is left: {bool(gone)}")
    assert gone, _toasts(shell)
    time.sleep(max(0.0, 7.0 - (time.monotonic() - t0)))
    assert [w["id"] for w in _toasts(shell)] == [held["id"]]          # still there at 7 s
    inject.move(shell, 400, 400)
    t_leave = time.monotonic()
    assert _wait(lambda: not _toasts(shell), 5, 0.05)
    life = time.monotonic() - t_leave
    print(f"hovered card closed {life:.2f} s after the pointer left")
    assert 2.5 * 0.95 - 0.05 <= life <= 2.5 * 1.05 + 0.7      # a coarse QTimer: ±5 %
    assert started["proc"].poll() is None


DRIVER = r"""
import json, sys, time
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from dragshift import report, toasts

def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

out = Path(sys.argv[1])
out.write_bytes(b"x")
card = toasts.ToastStack().new()
card.done(report.Result(title="Converted", detail="photo.png → photo.jpg", outs=[out]))
print(json.dumps({"t": time.time()}), flush=True)
spin(8000)
"""


def test_done_card_lives_six_and_a_half_seconds(started):
    s, shell = started["session"], started["shell"]
    inject.move(shell, 400, 400)
    p = s.popen([sys.executable, "-c", DRIVER, str(s.root / "tmp" / "photo.jpg")], cwd=ROOT,
                env={"PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, text=True)
    try:
        t0 = json.loads(p.stdout.readline())["t"]
        card = _wait(lambda: _toasts(shell), 5)
        assert card and _near(card[0]["frame"], _slot(started["area"], 0)), card
        assert _wait(lambda: not _toasts(shell), 9, 0.05)
        life = time.time() - t0
        print(f"done card closed after {life:.2f} s")
        assert 6.5 * 0.95 - 0.05 <= life <= 6.5 * 1.05 + 0.7   # a coarse QTimer: ±5 %
    finally:
        p.kill()
        p.wait()


SECOND = (1280, 1024)


def test_cards_go_to_the_pointers_monitor(built_dir):  # noqa: F811
    """Two virtual monitors (1600x900 and 1280x1024 to its right): with the pointer on the
    second one, the cards stack at the bottom right of the second one's work area."""
    require_session_tools()
    with Session(extensions=[built_dir], helper=True,
                 shell_args=["--virtual-monitor", f"{SECOND[0]}x{SECOND[1]}"]) as s, \
            ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(3.0)
        mon1, area1 = shell.work_area(800, 450)
        mon2, area2 = shell.work_area(mon1[2] + 100, 100)
        print(f"\nmonitors {mon1} {mon2}, work areas {area1} {area2}")
        if mon2 == mon1:
            pytest.skip("the session started with one monitor")
        proc = _start_app(s)
        try:
            inject.move(shell, mon2[0] + 300, mon2[1] + 300)
            _handoffs(s, 2)
            cards = _settled(shell, 2, [_slot(area2, 0), _slot(area2, 1)])
            print(f"cards: {[(w['title'], w['frame']) for w in cards]}")
            assert len(cards) == 2
            for i, w in enumerate(cards):
                assert _near(w["frame"], _slot(area2, i)) and _inside(w["frame"], area2)
            img = Image.open(shell.screenshot(s.root / "tmp" / "two-monitors.png"))
            _evidence("toasts-second-monitor.png", img)
            # back on the first monitor: the next card takes the stack along
            inject.move(shell, 400, 400)
            _handoffs(s, 1)
            cards = _settled(shell, 3, [_slot(area1, i) for i in range(3)])
            print(f"after a card on monitor 1: {[(w['title'], w['frame']) for w in cards]}")
            assert len(cards) == 3
            for i, w in enumerate(cards):
                assert _near(w["frame"], _slot(area1, i)), (w["frame"], _slot(area1, i))
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
