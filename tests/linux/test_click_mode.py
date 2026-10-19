"""Click mode (#36) in the isolated GNOME session, with the real app (`python -m dragshift`)
and DragShift's extension: the menu opened without a drag.

- A file handed over by a second `python -m dragshift <file>` (Files' Open With and the
  CLI use this IPC path) opens the menu at the pointer. Hovering a petal highlights it,
  clicking PNG converts.
- A click outside, a right-click and Escape close it without output. Escape is grabbed
  only while the menu is open.
- A click on the hub flips to the Tools ring.
- Two launches within 280 ms give one menu for both files. A launch while DragShift is
  not running starts it and opens the menu.
- The tray's "Open a file…" opens the file picker. The path chosen there is a real path:
  the menu opens for it and the conversion lands next to it.

Input is injected with tests/linux/inject.py. A button is held for HOLD seconds, since
outside clicks are seen by polling the extension's pointer state (radial.CLICK_POLL_MS).

With DRAGSHIFT_EVIDENCE_DIR set, screenshots are written there."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from dragshift import app as dsapp
from dragshift import platform_linux as lx
from dragshift.radial import MENU_TITLE, R_IN, R_OUT, SIZE, angles
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session, gi_python
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (_has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.linux.test_menu_place import expected_tl
from tests.linux.test_pointer import _reset_client
from tests.linux.test_startup import TRAY_CLICK

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
POINTER = (700, 420)        # where the user is when a file comes in; the menu fits around it
OUTSIDE = (1150, 250)       # on the desktop, away from the menu and DING's icons
HOLD = 0.08                 # s a button is held (a quick human click)
PICKER = "Choose file(s) for DragShift"
TOLERANCE = 2


def _evidence(name: str, path: Path) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        shutil.copy(path, Path(out) / name)


def _jpeg(path: Path, color=(200, 40, 40)) -> Path:
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=95)
    return path


# ------------------------------------------------------------------ session and app
@pytest.fixture(scope="module", params=[1, 2], ids=["scale1", "scale2"])
def env(request, built_dir):  # noqa: F811
    """The session at 100 % and at the user's 200 % (1600x900 logical either way)."""
    require_session_tools()
    scale = request.param
    with Session(size=(1600 * scale, 900 * scale), scale=scale, extensions=[built_dir],
                 helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        assert _wait(lambda: _has_owner(s)), _state(s)
        time.sleep(3.0)     # the shell's boot banner goes away
        e = s.env()
        conf = Path(e["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("[General]\nwelcomed=true\n")   # no Welcome window over the menu
        yield SimpleNamespace(session=s, shell=shell, scale=scale, home=Path(e["HOME"]),
                              socket=Path(e["TMPDIR"]) / ("DragShift-" + (
                                  e.get("USERNAME") or e.get("USER") or "user")),
                              log=Path(e["XDG_STATE_HOME"]) / "DragShift" / "log.txt")
        shell.release_all()


@pytest.fixture
def folder(env):
    d = env.home / f"pics-{time.monotonic_ns()}"
    d.mkdir()
    _jpeg(d / "Photo.jpg")
    return d


class App:
    """DragShift running in the session as a user starts it (`python -m dragshift`)."""

    def __init__(self, env, *args: str, extra_env: dict | None = None):
        self.env, self.out = env, env.session.root / "tmp" / f"app-{time.monotonic_ns()}.out"
        env.socket.unlink(missing_ok=True)       # a SIGTERMed app leaves its socket behind
        with open(self.out, "w") as f:
            self.proc = env.session.popen([sys.executable, "-m", "dragshift", *args],
                                          cwd=ROOT, env={"PYTHONPATH": str(ROOT),
                                                         **(extra_env or {})},
                                          stdout=f, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        while not env.socket.exists():
            assert self.proc.poll() is None, self.text()
            assert time.monotonic() < deadline, "the app never listened: " + self.text()
            time.sleep(0.05)

    def text(self) -> str:
        return self.out.read_text(errors="replace")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.env.shell.release_all()


@pytest.fixture
def app(env):
    a = App(env, "--quiet")
    try:
        yield a
    finally:
        a.stop()


def hand_over(env, *paths: Path) -> subprocess.CompletedProcess:
    """`python -m dragshift <files>` while DragShift runs: hands them over and exits."""
    r = env.session.run([sys.executable, "-m", "dragshift", *map(str, paths)], cwd=ROOT,
                        env={"PYTHONPATH": str(ROOT)}, timeout=20)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


# ------------------------------------------------------------------ the menu on screen
def menu_window(env) -> dict | None:
    return next((w for w in env.shell.list_windows() if w["title"] == MENU_TITLE), None)


def wait_menu(env, at=POINTER, timeout: float = 10.0) -> dict:
    """The menu once it is mapped where it belongs (centred on `at`, as #34 clamps it) and
    its pop-in animation is over; `seen` is when the shell first listed it there."""
    tl = expected_tl(*at)
    deadline = time.monotonic() + timeout
    while True:
        w = menu_window(env)
        if w and abs(w["frame"][0] - tl[0]) <= TOLERANCE and abs(w["frame"][1] - tl[1]) <= TOLERANCE:
            w["seen"] = time.monotonic()
            time.sleep(0.4)
            return w
        assert time.monotonic() < deadline, f"menu not at {tl}: {w}"
        time.sleep(0.03)


def wait_closed(env, timeout: float = 2.0) -> float:
    """Seconds until the menu is gone."""
    t0 = time.monotonic()
    while menu_window(env) is not None:
        assert time.monotonic() - t0 < timeout, "the menu is still open"
        time.sleep(0.01)
    return time.monotonic() - t0


def petal(win: dict, n: int, i: int) -> tuple[int, int]:
    """Screen centre of petal i of n (radial.angles: 0 at 12 o'clock, clockwise)."""
    import math
    x, y = win["frame"][:2]
    a0, a1 = angles(n, i)
    mid, r = math.radians((a0 + a1) / 2), (R_IN + R_OUT) / 2
    return round(x + SIZE / 2 + r * math.cos(mid)), round(y + SIZE / 2 - r * math.sin(mid))


def hub(win: dict) -> tuple[int, int]:
    return win["frame"][0] + SIZE // 2, win["frame"][1] + SIZE // 2


def click(env, x, y, button="left") -> float:
    """Glide to (x, y), settle, press for HOLD s and release; returns when it was pressed."""
    inject.glide(env.shell, x, y, 0.25)
    time.sleep(0.15)
    inject.button_down(env.shell, button)
    pressed = time.monotonic()
    time.sleep(HOLD)
    inject.button_up(env.shell, button)
    return pressed


def rings(path: Path) -> dict:
    """What the app puts on the menu for `path` (same code, same Pillow)."""
    return {k: [it.label for it in v]
            for k, v in dsapp.DragShiftApp.build_rings(None, [str(path)]).items()}


def shot(env, name: str) -> Image.Image:
    """A screenshot (device pixels) of the whole session."""
    name = f"{name}-scale{env.scale}"
    p = env.shell.screenshot(env.session.root / "tmp" / f"{name}.png")
    _evidence(f"{name}.png", p)
    return Image.open(p).convert("RGB")


def wait_file(path: Path, timeout: float = 15.0) -> Path:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.1)
    time.sleep(0.3)
    return path


def open_menu(env, folder: Path) -> dict:
    inject.glide(env.shell, *POINTER, 0.25)
    hand_over(env, folder / "Photo.jpg")
    return wait_menu(env)


# ------------------------------------------------------------------ the tests
def test_handed_over_file_opens_the_menu_at_the_pointer_and_png_converts(env, app, folder):
    inject.glide(env.shell, *POINTER, 0.25)
    t0 = time.monotonic()
    hand_over(env, folder / "Photo.jpg")
    win = wait_menu(env)
    opened = win["seen"] - t0
    assert win["above"] and win["pid"] == app.proc.pid
    labels = rings(folder / "Photo.jpg")["Convert"]
    i = labels.index("PNG")
    plain = shot(env, "ipc-menu")
    x, y = petal(win, len(labels), i)
    inject.glide(env.shell, x, y, 0.3)
    time.sleep(0.5)                                     # the hover spring settles
    hovered = shot(env, "ipc-menu-hover-png")
    box = tuple(v * env.scale for v in (x - 30, y - 20, x + 30, y + 20))
    diff = np.abs(np.asarray(hovered.crop(box), float) - np.asarray(plain.crop(box), float))
    print(f"\nmenu {opened * 1000:.0f} ms after the hand-over at {win['frame']}; "
          f"hover changes the PNG petal by {diff.mean():.1f} levels")
    assert diff.mean() > 8, "hovering did not highlight the petal"
    click(env, x, y)
    wait_closed(env)
    out = wait_file(folder / "Photo.png")
    with Image.open(out) as im:
        assert im.format == "PNG" and im.size == (64, 48)
    assert (folder / "Photo.jpg").exists()
    assert "IPC: 1 file(s) handed over" in app.text()


def test_click_outside_closes_without_output(env, app, folder):
    """The menu goes on the press, as a popup does, while the button is still down."""
    open_menu(env, folder)
    inject.glide(env.shell, *OUTSIDE, 0.25)
    time.sleep(0.15)
    inject.button_down(env.shell)
    try:
        took = wait_closed(env)
    finally:
        inject.button_up(env.shell)
    time.sleep(1.0)
    assert sorted(p.name for p in folder.iterdir()) == ["Photo.jpg"]
    assert f"menu: click outside at ({OUTSIDE[0]}, {OUTSIDE[1]}), closing" in app.text()
    print(f"\nclick outside: the shell stopped listing the menu {took * 1000:.0f} ms after "
          "the press")


@pytest.mark.parametrize("where", ["on-a-petal", "outside"])
def test_right_click_closes_without_output(env, app, folder, where):
    """Outside, the click also reaches what is under it, as every outside click does
    (nothing grabs it): on the desktop that opens DING's context menu, closed here."""
    win = open_menu(env, folder)
    n = len(rings(folder / "Photo.jpg")["Convert"])
    click(env, *(petal(win, n, 0) if where == "on-a-petal" else OUTSIDE), button="right")
    wait_closed(env)
    time.sleep(1.0)
    if where == "outside":
        shot(env, "right-click-outside")
        inject.press(env.shell, "Escape")          # DING's context menu
    assert sorted(p.name for p in folder.iterdir()) == ["Photo.jpg"]


@pytest.fixture
def client(env, monkeypatch):
    """platform_linux in this process, on the session's bus."""
    _reset_client()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", env.session.bus_address)
    assert _wait(lambda: lx._call("Version") is not None)
    try:
        yield lx
    finally:
        lx.watch_escape(False)
        _reset_client()


def test_escape_closes_and_is_grabbed_only_while_open(env, app, folder, client):
    """While the menu is open DragShift holds the Escape grab (a second watcher is
    refused). Escape closes it and gives the grab back."""
    assert client.watch_escape(True, lambda: None)      # free before the menu opens
    client.watch_escape(False)
    open_menu(env, folder)
    assert not client.watch_escape(True, lambda: None)  # the app holds it
    inject.glide(env.shell, *OUTSIDE, 0.2)              # Escape works wherever the pointer is
    inject.press(env.shell, "Escape")
    took = wait_closed(env)
    print(f"\nEscape closed the menu in {took * 1000:.0f} ms")
    assert _wait(lambda: client.watch_escape(True, lambda: None), 3)   # released
    client.watch_escape(False)
    assert sorted(p.name for p in folder.iterdir()) == ["Photo.jpg"]


def test_hub_click_flips_to_the_tools_ring(env, app, folder):
    win = open_menu(env, folder)
    r = rings(folder / "Photo.jpg")
    click(env, *hub(win))
    time.sleep(0.5)                                     # the ring-swap animation
    assert menu_window(env) is not None                 # still open
    shot(env, "hub-click-tools-ring")
    i = r["Tools"].index("Compress")
    click(env, *petal(win, len(r["Tools"]), i))
    wait_closed(env)
    tool = env.shell.wait_for_window(title="Compress — DragShift", timeout=15)
    assert tool["pid"] == app.proc.pid


def test_two_launches_within_280_ms_give_one_menu_for_both(env, app, folder):
    second = _jpeg(folder / "Second.jpg", (40, 160, 40))
    inject.glide(env.shell, *POINTER, 0.25)
    procs = [env.session.popen([sys.executable, "-m", "dragshift", str(p)], cwd=ROOT,
                               env={"PYTHONPATH": str(ROOT)})
             for p in (folder / "Photo.jpg", second)]
    assert [p.wait(20) for p in procs] == [0, 0]
    win = wait_menu(env)
    shot(env, "two-files-menu")
    assert app.text().count("IPC: 1 file(s) handed over") == 2
    assert app.text().count("menu: at") == 1            # one menu
    labels = rings(folder / "Photo.jpg")["Convert"]
    click(env, *petal(win, len(labels), labels.index("PNG")))
    wait_closed(env)
    wait_file(folder / "Photo.png")
    wait_file(folder / "Second.png")


def test_launch_with_a_file_when_not_running_starts_and_opens_the_menu(env, folder):
    inject.glide(env.shell, *POINTER, 0.25)
    started = App(env, str(folder / "Photo.jpg"))
    try:
        win = wait_menu(env, timeout=20)
        assert win["pid"] == started.proc.pid
        labels = rings(folder / "Photo.jpg")["Convert"]
        click(env, *petal(win, len(labels), labels.index("PNG")))
        wait_closed(env)
        wait_file(folder / "Photo.png")
    finally:
        started.stop()


# Which file picker Qt opens: by default (no QT_QPA_PLATFORMTHEME, GNOME) its GTK 3 theme
# shows GTK's chooser inside DragShift's process; with xdgdesktopportal the desktop portal
# shows GNOME's (xdg-desktop-portal-gnome) in its own process.
PICKERS = {"gtk": ({}, "python"),
           "portal": ({"QT_QPA_PLATFORMTHEME": "xdgdesktopportal"}, "xdg-desktop-portal-gnome")}


@pytest.mark.parametrize("picker", list(PICKERS))
def test_tray_open_a_file_picks_a_real_path(env, folder, picker):
    """Tray → "Open a file…" (as the top bar sends it over com.canonical.dbusmenu) opens
    the picker. A path typed there opens the menu at the pointer, and PNG writes next to
    the file, so the path that came back is the real one."""
    extra, wm_class = PICKERS[picker]
    a = App(env, "--quiet", extra_env=extra)
    try:
        _pick(env, a, folder, picker, wm_class)
    finally:
        a.stop()


def _pick(env, app, folder, picker, wm_class):
    s = env.session
    deadline = time.monotonic() + 15        # the tray item registers shortly after start-up
    while True:
        r = s.run([gi_python(), "-I", "-c", TRAY_CLICK, "Open a file…"])
        if r.returncode == 0 or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    assert r.returncode == 0, r.stdout + r.stderr
    dlg = env.shell.wait_for_window(title=PICKER, timeout=15)
    print(f"\n{picker} picker: {dlg['window_type']}, wm_class {dlg['wm_class']!r}, "
          f"{'DragShift' if dlg['pid'] == app.proc.pid else 'its own'} process")
    assert dlg["wm_class"] == wm_class
    assert (dlg["pid"] == app.proc.pid) == (picker == "gtk")
    time.sleep(1.5)
    shot(env, f"file-picker-{picker}")
    env.shell.activate_window(dlg["id"])
    time.sleep(0.3)
    inject.glide(env.shell, *POINTER, 0.25)
    inject.type_text(env.shell, str(folder / "Photo.jpg"))
    time.sleep(0.5)
    inject.press(env.shell, "Return")
    win = wait_menu(env)
    assert not any(w["title"] == PICKER for w in env.shell.list_windows())
    shot(env, f"file-picker-{picker}-menu")
    labels = rings(folder / "Photo.jpg")["Convert"]
    click(env, *petal(win, len(labels), labels.index("PNG")))
    wait_closed(env)
    wait_file(folder / "Photo.png")
