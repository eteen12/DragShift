"""Open With and the command line (#50) in the isolated GNOME session, with the real app.

DragShift is set up the way install-linux.sh (#66) lays it out, inside the session only:
the desktop entry packaging/linux/io.github.eteen12.DragShift.desktop in the session's
XDG_DATA_HOME/applications, its icon in XDG_DATA_HOME/icons, and the launcher
packaging/linux/dragshift in HOME/.local/bin, first on PATH, with
XDG_DATA_HOME/dragshift/{venv,src} pointing at this venv and this checkout.

- After update-desktop-database, GIO recommends the entry for every MIME type DragShift
  handles (`gio mime`).
- `dragshift a.jpg b.jpg` and `python -m dragshift a.jpg b.jpg` hand both files to the
  running app over $XDG_RUNTIME_DIR/DragShift, exit 0 in under a second, and one menu for
  "2 files" opens. With nothing listening, even on a socket a killed app left behind,
  the same command starts DragShift and the menu opens.
- Files: select two pictures, right-click → Open With… → DragShift → Open. The menu
  opens for both, from the running app or from a fresh start.

The menu's position is #34's and #36's (tests/linux/test_click_mode.py); here it only has
to appear, once, in the right process. With DRAGSHIFT_EVIDENCE_DIR set, screenshots are
written there."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dragshift import app as dsapp
from dragshift import handoff
from dragshift.radial import MENU_TITLE
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session, gi_python
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (_has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.test_handoff import MIME

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging" / "linux"
DESKTOP_ID = f"{dsapp.LINUX_APP_ID}.desktop"
POINTER = (700, 420)
CHOOSER = "Open File"           # Nautilus 46's Open With… dialog
# Nautilus 46.4's context menu for a multiple selection, in order: New Folder with
# Selection | Open With <default>, Open With… | Cut, Copy, Move to…, Copy to… | …
# Its GTK 4 menu items have no accessible names, so the item is found by position;
# the chooser dialog appearing proves it was the right one.
OPEN_WITH_ITEM = 2

# Runs with the system python (gi): activates the n-th item of Nautilus' open menu.
MENU_ITEM = r"""
import sys
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
items = []
def walk(node, in_menu):
    role = node.get_role_name()
    in_menu = in_menu or role == "menu"
    if in_menu and role == "menu item":
        items.append(node)
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            walk(child, in_menu)
desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    a = desktop.get_child_at_index(i)
    if a is not None and a.get_name() == "org.gnome.Nautilus":
        walk(a, False)
n = int(sys.argv[1])
if len(items) <= n:
    sys.exit(f"{len(items)} menu items")
print(len(items), items[n].get_action_iface().do_action(0))
"""


def _evidence(name: str, path: Path) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        shutil.copy(path, Path(out) / name)


class InSession:
    """The session with HOME/.local/bin first on PATH, as after a login with the launcher
    installed. Same interface as Session for tests.linux.files.Files."""

    def __init__(self, session, path: str):
        self.session, self.path = session, path
        self.root = session.root

    def env(self, **extra):
        return self.session.env(**{"PATH": self.path, **extra})

    def run(self, cmd, **kw):
        kw["env"] = {"PATH": self.path, **kw.get("env", {})}
        return self.session.run(cmd, **kw)

    def popen(self, cmd, **kw):
        kw["env"] = {"PATH": self.path, **kw.get("env", {})}
        return self.session.popen(cmd, **kw)


@pytest.fixture(scope="module")
def env(built_dir):  # noqa: F811
    require_session_tools()
    venv = Path(sys.prefix)
    assert (venv / "bin" / "python").exists(), "run the tests from the project venv"
    desktop = (PACKAGING / DESKTOP_ID).read_text()
    with Session(extensions=[built_dir], helper=True,
                 data_files={f"applications/{DESKTOP_ID}": desktop}) as s, \
            ShellHelper(s) as shell:
        shell.wait_ready()
        assert _wait(lambda: _has_owner(s)), _state(s)
        e = s.env()
        home, data = Path(e["HOME"]), Path(e["XDG_DATA_HOME"])
        bindir = home / ".local" / "bin"
        bindir.mkdir(parents=True)
        shutil.copy(PACKAGING / "dragshift", bindir / "dragshift")
        (data / "dragshift").mkdir()
        (data / "dragshift" / "venv").symlink_to(venv)
        (data / "dragshift" / "src").symlink_to(ROOT)
        icons = data / "icons" / "hicolor" / "256x256" / "apps"
        icons.mkdir(parents=True)
        with Image.open(ROOT / "dragshift" / "assets" / "dragshift.png") as im:
            im.resize((256, 256)).save(icons / f"{dsapp.LINUX_APP_ID}.png")
        inside = InSession(s, f"{bindir}:{e['PATH']}")
        # Files may be started by D-Bus activation: give that the same PATH
        r = s.run(["dbus-update-activation-environment", f"PATH={inside.path}"])
        assert r.returncode == 0, r.stderr
        conf = Path(e["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("[General]\nwelcomed=true\n")   # no Welcome window over the menu
        time.sleep(3.0)     # the shell's boot banner goes away
        yield SimpleNamespace(session=s, inside=inside, shell=shell, home=home, data=data,
                              socket=Path(handoff.socket_path(e)),
                              log=Path(e["XDG_STATE_HOME"]) / "DragShift" / "log.txt")
        shell.release_all()


@pytest.fixture
def pics(env):
    d = env.home / f"pics-{time.monotonic_ns()}"
    d.mkdir()
    for name, color in (("Photo.jpg", (200, 40, 40)), ("Second.jpg", (40, 160, 40))):
        Image.new("RGB", (64, 48), color).save(d / name, "JPEG", quality=95)
    return d


class App:
    """DragShift started by the launcher, as a user (or Files) starts it."""

    def __init__(self, env, *args: str, cwd=None, wait: bool = True):
        self.env = env
        self.out = env.session.root / "tmp" / f"app-{time.monotonic_ns()}.out"
        self.log_from = log_size(env)
        with open(self.out, "w") as f:
            self.proc = env.inside.popen(["dragshift", *args], cwd=cwd or env.home,
                                         stdout=f, stderr=subprocess.STDOUT)
        if wait:
            deadline = time.monotonic() + 20
            while f"ipc: listening at {env.socket}" not in self.log():
                assert self.proc.poll() is None, self.text()
                assert time.monotonic() < deadline, "the app never listened: " + self.text()
                time.sleep(0.05)

    def text(self) -> str:
        return self.out.read_text(errors="replace")

    def log(self) -> str:
        """log.txt since this app was started."""
        return log_since(self.env, self.log_from)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()       # like a crash for the socket: Qt leaves it behind
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.env.shell.release_all()


def log_size(env) -> int:
    return env.log.stat().st_size if env.log.exists() else 0


def log_since(env, offset: int) -> str:
    if not env.log.exists():
        return ""
    with open(env.log, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace")


def dragshift_processes(env) -> dict[int, list[str]]:
    """pid → argv of every DragShift app in this session."""
    found = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]
        except OSError:
            continue
        if argv[1:3] == ["-m", "dragshift"] and str(env.data) in argv[0]:
            found[int(pid)] = argv
    return found


def wait_menu(env, timeout: float = 15.0) -> dict:
    """The menu window once the shell has placed it."""
    deadline = time.monotonic() + timeout
    while True:
        w = next((w for w in env.shell.list_windows() if w["title"] == MENU_TITLE), None)
        if w and w["frame"][2] > 0:
            w["seen"] = time.monotonic()
            return w
        assert time.monotonic() < deadline, "no DragShift menu"
        time.sleep(0.02)


def close_menu(env) -> None:
    inject.press(env.shell, "Escape")
    deadline = time.monotonic() + 3
    while any(w["title"] == MENU_TITLE for w in env.shell.list_windows()):
        assert time.monotonic() < deadline, "the menu did not close"
        time.sleep(0.02)


def shot(env, name: str) -> None:
    p = env.shell.screenshot(env.session.root / "tmp" / f"{name}.png")
    _evidence(f"{name}.png", p)


# ------------------------------------------------------------------ the desktop entry
def test_files_recommends_dragshift_for_every_type(env):
    apps = env.data / "applications"
    r = env.inside.run(["update-desktop-database", str(apps)])
    assert r.returncode == 0, r.stderr
    cache = (apps / "mimeinfo.cache").read_text()
    for mime in sorted(set(MIME.values())):
        assert f"{mime}=" in cache and DESKTOP_ID in cache
        out = env.inside.run(["gio", "mime", mime]).stdout
        recommended = out.split("Recommended applications:")[-1].split()
        assert DESKTOP_ID in recommended, f"{mime}:\n{out}"
    print("\n" + env.inside.run(["gio", "mime", "image/png"]).stdout)


# ------------------------------------------------------------------ the command line
@pytest.mark.parametrize("how", ["launcher", "python -m"])
def test_cli_hands_both_files_to_the_running_app(env, pics, how):
    app = App(env, "--quiet")
    try:
        inject.glide(env.shell, *POINTER, 0.25)
        files = [str(pics / "Photo.jpg"), str(pics / "Second.jpg")]
        cmd = (["dragshift", *files] if how == "launcher" else
               [sys.executable, "-m", "dragshift", *files])
        t0 = time.monotonic()
        r = env.inside.run(cmd, cwd=pics, env={"PYTHONPATH": str(ROOT)}, timeout=20)
        took = time.monotonic() - t0
        assert r.returncode == 0, r.stdout + r.stderr
        win = wait_menu(env)
        opened = win["seen"] - t0
        print(f"\n{how}: exited 0 after {took * 1000:.0f} ms, menu {opened * 1000:.0f} ms "
              f"after the command started (280 ms of it is the coalescing timer)")
        assert took < 1.0
        assert win["pid"] == app.proc.pid
        assert list(dragshift_processes(env)) == [app.proc.pid]   # no second app
        time.sleep(0.5)
        shot(env, f"cli-{how.replace(' ', '')}-2-files")
        assert "IPC: 2 file(s) handed over" in app.text()
        assert app.text().count("menu: at") == 1
        assert app.log().count("click menu: ") == 1 and "click menu: 2 file(s)" in app.log()
        close_menu(env)
    finally:
        app.stop()


def test_cli_starts_dragshift_when_it_is_not_running(env, pics):
    """A killed app left its socket behind: the hand-off is refused at once, the command
    goes on to start DragShift, which takes the socket over and opens the menu."""
    App(env, "--quiet").stop()
    assert env.socket.exists() and not dragshift_processes(env)
    inject.glide(env.shell, *POINTER, 0.25)
    t0 = time.monotonic()
    # a relative path resolves against the caller's cwd; a missing file is dropped
    app = App(env, str(pics / "Photo.jpg"), "Second.jpg", "Missing.jpg", cwd=pics,
              wait=False)
    try:
        win = wait_menu(env, timeout=30)
        print(f"\ncold start: menu {(win['seen'] - t0) * 1000:.0f} ms after the command")
        assert win["pid"] == app.proc.pid
        time.sleep(0.5)
        shot(env, "cli-cold-start")
        assert f"ipc: listening at {env.socket}" in app.log()
        assert app.text().count("menu: at") == 1
        assert app.log().count("click menu: ") == 1 and "click menu: 2 file(s)" in app.log()
        close_menu(env)
    finally:
        app.stop()


# ------------------------------------------------------------------ Files: Open With
def open_with_dragshift(env, files: Files, folder: Path, tag: str) -> float:
    """Select both pictures, right-click → Open With… → DragShift → Open. Returns when
    Open was pressed."""
    files.open_folder(folder, view="icon")
    a, b = files.icon_center("Photo.jpg"), files.icon_center("Second.jpg")
    inject.click(env.shell, *a)
    time.sleep(0.3)
    with inject.hold_key(env.shell, "Control_L"):
        inject.click(env.shell, *b)
    time.sleep(0.3)
    inject.click(env.shell, *a, button="right")
    time.sleep(1.0)
    shot(env, f"open-with-{tag}-1-context-menu")
    r = env.inside.run([gi_python(), "-I", "-c", MENU_ITEM, str(OPEN_WITH_ITEM)])
    assert r.returncode == 0, r.stdout + r.stderr
    deadline = time.monotonic() + 15
    while True:     # listed before it is mapped: wait for its frame
        dlg = next((w for w in env.shell.list_windows() if w["title"] == CHOOSER), None)
        if dlg and dlg["frame"][2] > 0:
            break
        assert time.monotonic() < deadline, "no Open With dialog"
        time.sleep(0.05)
    time.sleep(0.8)
    x, y, w, h = dlg["frame"]
    inject.click(env.shell, x + w // 2, y + h // 2)     # into the app list
    time.sleep(0.4)
    inject.type_text(env.shell, "DragShift")            # the list's type-ahead search
    time.sleep(0.8)
    shot(env, f"open-with-{tag}-2-chooser")
    pressed = time.monotonic()
    inject.press(env.shell, "Return")                   # = Open
    return pressed


@pytest.mark.parametrize("running", [True, False], ids=["running", "cold"])
def test_files_open_with_dragshift(env, pics, running):
    app = App(env, "--quiet") if running else None
    if not running:
        assert not dragshift_processes(env)
    log_from = log_size(env)
    files = Files(env.inside, env.shell)
    try:
        t0 = open_with_dragshift(env, files, pics, "running" if running else "cold")
        win = wait_menu(env, timeout=30)
        print(f"\nOpen With ({'running' if running else 'cold start'}): menu "
              f"{(win['seen'] - t0) * 1000:.0f} ms after Open")
        procs = dragshift_processes(env)
        assert list(procs) == [win["pid"]]
        if running:
            assert win["pid"] == app.proc.pid
            assert "IPC: 2 file(s) handed over" in app.text()
        else:   # Files ran the desktop entry's Exec: dragshift %F → the launcher
            argv = procs[win["pid"]]
            assert argv[0] == str(env.data / "dragshift" / "venv" / "bin" / "python")
            assert argv[3:] == [str(pics / "Photo.jpg"), str(pics / "Second.jpg")]
        time.sleep(0.6)
        shot(env, f"open-with-{'running' if running else 'cold'}-3-menu")
        log = log_since(env, log_from)
        assert log.count("click menu: ") == 1 and "click menu: 2 file(s)" in log
        close_menu(env)
    finally:
        files.close()
        if app:
            app.stop()
        for pid in dragshift_processes(env):
            os.kill(pid, 15)
        deadline = time.monotonic() + 10
        while dragshift_processes(env) and time.monotonic() < deadline:
            time.sleep(0.1)
