"""The menu and toasts stay out of the dock and Alt+Tab (#95), with the real app.

Mutter lists every window of an ordinary Wayland client as a running app (dock icon,
Alt+Tab) and focuses it on map. DragShift therefore asks its shell extension to start it
as the shell's own Wayland client (`Launch`, extension/.../launcher.js; the client side is
`handoff.start_managed`), and the extension makes the windows it places (the menu, the
toasts) dock-type before they map. These tests run the real app in the isolated session:

- a start from the command line, the app grid or the autostart entry becomes one managed
  app; files are handed to it;
- while the menu and a toast are up: no running DragShift app (`get_running()`), neither
  window in Alt+Tab (`get_tab_list`), Files keeps the focus;
- without the launcher or with the extension off, the app runs on its own as before;
- a hung managed app is killed when GNOME's "not responding" dialog shows, because that
  dialog's Force Quit would kill gnome-shell (the windows report its pid).

With DRAGSHIFT_EVIDENCE_DIR set, screenshots and numbers are written there.
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dragshift.radial import MENU_TITLE
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_display_backend import PHOTO, _focused
from tests.linux.test_extension import (UUID, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
APP_ID = "io.github.eteen12.DragShift.desktop"
DESKTOP = ("[Desktop Entry]\nType=Application\nName=DragShift\nExec=dragshift %F\n"
           "Icon=dragshift\nTerminal=false\n")
STARTED = re.compile(r"DragShift: started the app \(pid (\d+)\)")
TOAST = re.compile(r"^DragShift toast \d+$")


def _evidence(name: str, data=None, path: Path | None = None) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if not out:
        return
    if path is not None:
        shutil.copy(path, Path(out) / name)
    else:
        Path(out, name).write_text(json.dumps(data, indent=1))


@pytest.fixture(scope="module")
def session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(extensions=[built_dir], helper=True,
                 data_files={f"applications/{APP_ID}": DESKTOP}) as s:
        conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("[General]\nwelcomed=true\n")
        yield s


@pytest.fixture(scope="module")
def shell(session):
    with ShellHelper(session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(session)), _state(session)
        yield helper


def _launcher_path(session) -> Path:
    return Path(session.env()["HOME"]) / ".local" / "bin" / "dragshift"


@pytest.fixture
def launcher(session):
    """~/.local/bin/dragshift in the session's HOME, as #50's template: exec Python."""
    path = _launcher_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ncd {shlex.quote(str(ROOT))} && "
                    f"exec {shlex.quote(sys.executable)} -m dragshift \"$@\"\n")
    path.chmod(0o755)
    yield path
    path.unlink(missing_ok=True)


class App:
    """Starts DragShift the way a user does (`python -m dragshift`) and finds the copy the
    extension started (its pid from the shell log)."""

    def __init__(self, session, shell):
        self.session, self.shell = session, shell
        self._log_from = session.log_path.stat().st_size
        self.out = session.root / "tmp" / f"dragshift-{time.monotonic_ns()}.out"
        self.procs = []

    def start(self, *args, wait=True):
        with open(self.out, "ab") as f:
            p = self.session.popen([sys.executable, "-m", "dragshift", *args], cwd=ROOT,
                                   stdout=f, stderr=subprocess.STDOUT)
        self.procs.append(p)
        if wait:
            p.wait(30)
        return p

    def started_pids(self) -> list[int]:
        with open(self.session.log_path, "rb") as f:
            f.seek(self._log_from)
            text = f.read().decode("utf-8", "replace")
        return [int(m) for m in STARTED.findall(text)]

    def managed_pid(self, timeout=20) -> int:
        assert _wait(lambda: self.started_pids(), timeout), self.log()
        return self.started_pids()[-1]

    def log(self) -> str:
        return self.out.read_text() if self.out.exists() else ""

    def stop(self):
        self.shell.release_all()
        pids = [p.pid for p in self.procs if p.poll() is None] + self.started_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _wait(lambda: not any(Path(f"/proc/{pid}").exists() and _alive(pid) for pid in pids),
              10)
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        # A stopped app's windows go with it at once. (They stayed for seconds while the
        # extension's SubprocessLauncher still held the app's end of the Wayland socket.)
        assert _wait(lambda: not _ours(self.shell), 2), \
            (_ours(self.shell), [(pid, _alive(pid)) for pid in pids])


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except (OSError, IndexError):
        return False


@pytest.fixture
def app(session, shell):
    a = App(session, shell)
    yield a
    a.stop()


@pytest.fixture
def files_window(session, shell):
    files = Files(session, shell)
    folder = files.make_folder(f"pics-{time.monotonic_ns()}", {"Photo.jpg": PHOTO})
    win = files.open_folder(folder)
    shell.activate_window(win["id"])
    assert _wait(lambda: _focused(shell) == [win["title"]], 5), _focused(shell)
    yield win, folder
    files.close()


def _ours(shell) -> dict:
    return {w["title"]: w for w in shell.list_windows()
            if w["title"] == MENU_TITLE or TOAST.match(w["title"])}


def _dragshift_processes(session) -> list[int]:
    """Live `python -m dragshift` processes of this session (its runtime dir in their
    environment)."""
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            env = (d / "environ").read_bytes()
        except OSError:
            continue
        if b"-m" in argv and b"dragshift" in argv and \
                str(session.runtime_dir).encode() in env and _alive(int(d.name)):
            found.append(int(d.name))
    return found


# ------------------------------------------------------------------ the acceptance check
def test_menu_and_toast_stay_out_of_the_dock_and_alt_tab(session, shell, launcher, app,
                                                         files_window):
    """Started as a user starts it, the app runs as the extension's client. While its
    start-up toast and the menu are up, GNOME counts no running DragShift app, Alt+Tab
    lists neither window, both are dock-type and above, and Files keeps the focus."""
    win, _ = files_window
    starter = app.start()                     # app grid / command line, no files
    assert starter.returncode == 0, app.log()
    pid = app.managed_pid()
    assert _wait(lambda: any(TOAST.match(t) for t in _ours(shell)), 15), app.log()
    inject.glide(shell, 1140, 340, dur=0.2)
    inject.button_down(shell)
    try:
        inject.glide(shell, 1100, 300, dur=0.3)
        with inject.hold_key(shell, "Shift_L"):
            assert _wait(lambda: MENU_TITLE in _ours(shell), 5), app.log()
            time.sleep(0.3)
            ours = _ours(shell)
            running = shell.running_apps()
            focused = _focused(shell)
            tab = [w["title"] for w in shell.list_windows() if w["in_tab_list"]]
            shot = shell.screenshot(session.root / "tmp" / "dock-hide.png")
    finally:
        inject.button_up(shell)
    record = dict(managed_pid=pid, running=running, focused=focused, alt_tab=tab,
                  windows={t: {k: w[k] for k in ("window_type", "in_tab_list", "skip_taskbar",
                                                 "above", "focused", "frame", "app", "pid")}
                           for t, w in ours.items()})
    print("\n" + json.dumps(record, indent=1))
    _evidence("dock-hide.json", record)
    _evidence("dock-hide.png", path=shot)
    assert MENU_TITLE in ours and any(TOAST.match(t) for t in ours), ours
    assert APP_ID not in running, running
    assert focused == [win["title"]]
    for title, w in ours.items():
        assert w["window_type"] == "dock" and not w["in_tab_list"], (title, w)
        assert w["above"] and not w["focused"], (title, w)
        assert w["pid"] == session.shell_pid       # what makes windowPid() necessary
    assert _dragshift_processes(session) == [pid]
    # the extension placed the menu (PlaceWindow matched the managed pid): at the pointer
    menu = ours[MENU_TITLE]["frame"]
    assert abs(menu[0] + menu[2] // 2 - 1100) <= 2 and abs(menu[1] + menu[3] // 2 - 300) <= 2


def test_command_line_hands_files_to_the_managed_app(session, shell, launcher, app):
    """`dragshift <file>` cold-starts the managed app with the file (its menu opens, out of
    the dock); a second `dragshift <file>` hands over to it, and nothing starts twice."""
    photo = Path(session.env()["HOME"]) / f"cli-{time.monotonic_ns()}.jpg"
    photo.write_bytes(PHOTO)
    assert app.start(str(photo)).returncode == 0, app.log()
    pid = app.managed_pid()
    assert _wait(lambda: MENU_TITLE in _ours(shell), 15), app.log()
    assert _ours(shell)[MENU_TITLE]["window_type"] == "dock"
    assert APP_ID not in shell.running_apps()
    t0 = time.monotonic()
    second = app.start(str(photo))
    took = time.monotonic() - t0
    assert second.returncode == 0 and took < 3, (took, app.log())
    assert _wait(lambda: MENU_TITLE in _ours(shell), 5), app.log()
    assert app.started_pids() == [pid]
    assert _dragshift_processes(session) == [pid]
    print(f"\nhand-off to the running app took {took * 1000:.0f} ms")


def test_simultaneous_starts_make_one_app(session, shell, launcher, app):
    """Two launches at once (say autostart and a click in the app grid at login): the
    extension starts one app, both launchers hand over to it and exit."""
    a = app.start("--quiet", wait=False)
    b = app.start("--quiet", wait=False)
    assert a.wait(30) == 0 and b.wait(30) == 0, app.log()
    pid = app.managed_pid()
    time.sleep(1.0)
    assert app.started_pids() == [pid]
    assert _dragshift_processes(session) == [pid]


def test_autostart_entry_starts_the_managed_app(session, shell, launcher, app):
    """The XDG autostart entry (#47) runs the launcher with --quiet: it becomes the one
    managed app, with no start-up toast."""
    env = session.env()
    r = subprocess.run([sys.executable, "-c",
                        "from dragshift import platform_linux as p; p.set_autostart(True); "
                        "print(p.autostart_path())"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    entry = Path(r.stdout.strip())
    try:
        exec_line = next(ln[5:] for ln in entry.read_text().splitlines()
                         if ln.startswith("Exec="))
        argv = shlex.split(exec_line)
        assert argv == [str(launcher), "--quiet"], argv
        with open(app.out, "ab") as f:
            p = session.popen(argv, stdout=f, stderr=subprocess.STDOUT)
        app.procs.append(p)
        assert p.wait(30) == 0, app.log()
        pid = app.managed_pid()
        time.sleep(1.5)
        assert _dragshift_processes(session) == [pid]
        assert not _ours(shell), _ours(shell)            # quiet: no toast
    finally:
        entry.unlink(missing_ok=True)


def test_without_the_launcher_the_app_runs_on_its_own(session, shell, app):
    """No ~/.local/bin/dragshift (a source checkout): Launch says no and the app runs in
    the process the user started, as before; its toast is an ordinary window again."""
    assert not _launcher_path(session).exists()
    p = app.start(wait=False)
    assert _wait(lambda: any(TOAST.match(t) for t in _ours(shell)), 20), app.log()
    assert p.poll() is None and app.started_pids() == []
    toast = next(w for t, w in _ours(shell).items() if TOAST.match(t))
    assert toast["pid"] == p.pid and toast["window_type"] == "normal"


def test_with_the_extension_off_the_app_runs_on_its_own(session, shell, launcher, app):
    """Extension disabled: the app runs unmanaged in the started process."""
    assert session.run(["gnome-extensions", "disable", UUID]).returncode == 0
    try:
        assert _wait(lambda: not _has_owner(session), 10)
        p = app.start("--quiet", wait=False)
        time.sleep(4)
        assert p.poll() is None, app.log()
        assert app.started_pids() == [] and _dragshift_processes(session) == [p.pid]
    finally:
        assert session.run(["gnome-extensions", "enable", UUID]).returncode == 0
        assert _wait(lambda: _has_owner(session), 10)


def test_managed_app_survives_an_extension_restart(session, shell, launcher, app):
    """GNOME disables extensions while the screen is locked. The app keeps running, and
    after the extension is back its next menu is dock-type again."""
    assert app.start("--quiet").returncode == 0, app.log()
    pid = app.managed_pid()
    time.sleep(1.5)
    assert session.run(["gnome-extensions", "disable", UUID]).returncode == 0
    assert _wait(lambda: not _has_owner(session), 10)
    assert session.run(["gnome-extensions", "enable", UUID]).returncode == 0
    assert _wait(lambda: _has_owner(session), 10)
    assert _alive(pid)
    time.sleep(6)       # the app's D-Bus client backs off for up to 5 s after failures
    inject.glide(shell, 1140, 340, dur=0.2)
    inject.button_down(shell)
    try:
        inject.glide(shell, 1100, 300, dur=0.3)
        with inject.hold_key(shell, "Shift_L"):
            assert _wait(lambda: MENU_TITLE in _ours(shell), 5), app.log()
            menu = _ours(shell)[MENU_TITLE]
    finally:
        inject.button_up(shell)
    assert menu["window_type"] == "dock" and not menu["in_tab_list"], menu
    assert APP_ID not in shell.running_apps()


def test_hung_app_is_killed_before_force_quit_can_kill_the_shell(session, shell, launcher,
                                                                 app):
    """The managed app stops answering while the user clicks its toast. GNOME's "not
    responding" dialog would offer Force Quit, which kills the pid the window reports,
    gnome-shell's. The extension kills DragShift as the dialog appears; the shell lives."""
    assert app.start().returncode == 0, app.log()
    pid = app.managed_pid()
    assert _wait(lambda: any(TOAST.match(t) for t in _ours(shell)), 15), app.log()
    time.sleep(1.0)                           # let it finish sliding in
    os.kill(pid, signal.SIGSTOP)
    try:
        deadline = time.monotonic() + 30
        while _alive(pid) and time.monotonic() < deadline:
            toast = next((w for t, w in _ours(shell).items() if TOAST.match(t)), None)
            if toast:
                x, y, w, h = toast["frame"]
                inject.click(shell, x + w // 2, y + h // 2)
            time.sleep(1.0)
        shot = shell.screenshot(session.root / "tmp" / "hang.png")
        _evidence("hang-guard.png", path=shot)
        print("\n" + "\n".join(ln for ln in session.log_path.read_text().splitlines()
                                if "DragShift" in ln or "JS ERROR" in ln))
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    assert not _alive(pid)
    assert _alive(session.shell_pid)
    assert shell.list_windows() is not None             # the shell still answers
    assert _wait(lambda: not _ours(shell), 5)
    assert "stopped responding; killing it" in session.log_path.read_text()


@pytest.mark.parametrize("kind", ["symlink", "group-writable", "not-executable", "directory"])
def test_launch_refuses_a_launcher_it_cannot_trust(session, shell, launcher, app, kind):
    """Launch only starts ~/.local/bin/dragshift when it is a regular, executable file of
    this user that only the user can write; anything else is refused and nothing starts."""
    real = launcher.with_name("real-dragshift")
    launcher.rename(real)
    try:
        if kind == "symlink":
            launcher.symlink_to(real)
        elif kind == "directory":
            launcher.mkdir()
        else:
            shutil.copy(real, launcher)
            launcher.chmod(0o775 if kind == "group-writable" else 0o644)
        r = session.run([sys.executable, "-m", "tests.linux.caller", "launch"], cwd=ROOT)
        assert json.loads(r.stdout) == ["ok", [False]], (r.stdout, r.stderr)
        time.sleep(0.5)
        assert app.started_pids() == []
    finally:
        if launcher.is_dir() and not launcher.is_symlink():
            launcher.rmdir()
        else:
            launcher.unlink(missing_ok=True)
        real.rename(launcher)

