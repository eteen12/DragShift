"""The isolated headless GNOME Shell session (tests/linux/session.py)."""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.linux.conftest import REAL_ENV, require_session_tools
from tests.linux import session as session_mod
from tests.linux.session import (MARKER, SYSTEM_EXTENSIONS, UBUNTU_DEFAULT_EXTENSIONS, Session,
                                 SessionError, _pids_with_runtime_dir, reap_stale_sessions,
                                 ubuntu_mode)

ROOT = Path(__file__).resolve().parents[2]
SYSTEM_DING = "gjs /usr/share/gnome-shell/extensions/ding@rastersoft.com/app/ding.js"

gnome = pytest.mark.gnome


def _cmdlines():
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                yield int(entry.name), (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            except OSError:
                continue


def _system_ding_pids():
    """DING processes running the system copy: those belong to the real desktop."""
    return {pid for pid, cmd in _cmdlines() if cmd.startswith(SYSTEM_DING)}


def _names(session):
    r = session.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                     "--object-path", "/org/freedesktop/DBus",
                     "--method", "org.freedesktop.DBus.ListNames"], check=True)
    return set(re.findall(r"'([^']+)'", r.stdout))


def _wait_for(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    return fn()


def _assert_gone(session_dirs, runtime_dir):
    for d in session_dirs:
        assert not Path(d).exists(), f"{d} left behind"
    assert _pids_with_runtime_dir(str(runtime_dir)) == []


# --------------------------------------------------------------------------- session
@gnome
def test_shell_version(gnome_session):
    assert gnome_session.shell_version == "46.0"


@gnome
def test_remote_desktop_on_private_bus(gnome_session):
    assert "org.gnome.Mutter.RemoteDesktop" in _names(gnome_session)
    assert gnome_session.bus_address != REAL_ENV["DBUS_SESSION_BUS_ADDRESS"]


@gnome
def test_display_is_the_sessions_xwayland(gnome_session):
    s = gnome_session
    assert re.fullmatch(r":\d+", s.display or "")
    assert s.env()["DISPLAY"] == s.display != REAL_ENV["DISPLAY"]
    assert Path(f"/tmp/.X11-unix/X{s.display[1:]}").exists()
    if shutil.which("xdpyinfo"):
        r = s.run(["xdpyinfo"], timeout=15)
        assert r.returncode == 0 and f"name of display:    {s.display}" in r.stdout, r.stderr


@gnome
def test_real_desktop_untouched(gnome_session):
    s = gnome_session
    for k, v in REAL_ENV.items():
        assert os.environ.get(k) == v, f"{k} changed in the test process"
    env = s.env()
    assert env["WAYLAND_DISPLAY"] == s.wayland_display != REAL_ENV["WAYLAND_DISPLAY"]
    assert env["XDG_RUNTIME_DIR"] == str(s.runtime_dir) != REAL_ENV["XDG_RUNTIME_DIR"]
    assert env["HOME"].startswith(str(s.root))
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "TMPDIR"):
        assert env[k].startswith(str(s.root))
    # An app in the session gets the private TMPDIR, so e.g. DragShift's QLocalServer
    # socket can never collide with the real desktop's /tmp/DragShift-<user>.
    r = s.run(["sh", "-c", 'echo "$TMPDIR"; mktemp'], check=True)
    tmpdir, made = r.stdout.split()
    assert tmpdir == f"{s.root}/tmp" and made.startswith(tmpdir + "/")
    assert env["DBUS_SYSTEM_BUS_ADDRESS"].startswith(f"unix:path={s.runtime_dir}/")


@gnome
def test_runtime_dir_is_private_and_short(gnome_session):
    s = gnome_session
    assert stat.S_IMODE(s.runtime_dir.stat().st_mode) == 0o700
    sock = s.runtime_dir / s.wayland_display
    assert sock.is_socket() and len(str(sock)) < 108


@gnome
def test_extensions_list_contains_ding_from_private_copy(gnome_session):
    s = gnome_session
    r = s.run(["gnome-extensions", "list"], check=True)
    assert "ding@rastersoft.com" in r.stdout.split()
    info = s.run(["gnome-extensions", "info", "ding@rastersoft.com"], check=True).stdout
    assert f"Path: {s.root}/share/gnome-shell/extensions/ding@rastersoft.com" in info


@gnome
def test_ubuntu_default_extensions_active(gnome_session):
    """Dock, appindicators and DING run as on the host, with or without --mode=ubuntu."""
    s = gnome_session
    installed = [u for u in UBUNTU_DEFAULT_EXTENSIONS if (SYSTEM_EXTENSIONS / u).is_dir()]
    assert "ding@rastersoft.com" in installed
    for uuid in installed:
        info = _wait_for(lambda: "ACTIVE" in (r := s.run(
            ["gnome-extensions", "info", uuid])).stdout and r.stdout)
        assert info, f"{uuid} is not active in mode {s.mode!r}"


@gnome
def test_nautilus_starts_in_the_session(gnome_session, tmp_path):
    s = gnome_session
    p = s.popen(["nautilus", "--new-window", str(tmp_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(3)
        mine = set(_pids_with_runtime_dir(str(s.runtime_dir)))
        nautilus = [pid for pid, cmd in _cmdlines()
                    if pid in mine and cmd.split(" ")[0].endswith("nautilus")]
        assert nautilus, "no nautilus process alive in the session after 3 s"
        assert "org.gnome.Nautilus" in _names(s)
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0]
    assert "Failed to open display" not in out
    assert "Failed to open display" not in s.log_path.read_text(errors="replace")


# ------------------------------------------------------------------ lifecycle
@gnome
def test_parallel_session_and_teardown(gnome_session):
    """A second session next to the shared one: own names, real DING untouched, clean stop."""
    real_ding = _system_ding_pids()
    with Session() as s2:
        assert s2.wayland_display != gnome_session.wayland_display
        assert s2.runtime_dir != gnome_session.runtime_dir
        assert s2.display != gnome_session.display
        assert s2.bus_address != gnome_session.bus_address
        assert s2.shell_version == "46.0"
        private_ding = f"gjs {s2.root}/share/gnome-shell/extensions/ding@rastersoft.com/"
        assert _wait_for(lambda: any(c.startswith(private_ding) for _, c in _cmdlines()), 20), \
            "the session's DING copy did not start"
        time.sleep(1)
        dirs, rt, shell_pid = (s2.root, s2.runtime_dir), s2.runtime_dir, s2.shell_pid
    assert real_ding <= _system_ding_pids(), "the real desktop's DING was restarted"
    _assert_gone(dirs, rt)
    assert not Path(f"/proc/{shell_pid}").exists()
    for lock in Path("/tmp").glob(".X*-lock"):  # Xwayland lock/socket cleaned up too
        assert lock.read_text().strip() != str(shell_pid), f"{lock} left behind"


@gnome
def test_caller_extension_is_installed_and_enabled(tmp_path):
    require_session_tools()
    uuid = "probe@dragshift.test"
    ext = tmp_path / uuid
    ext.mkdir()
    (ext / "metadata.json").write_text(json.dumps(
        {"uuid": uuid, "name": "Probe", "description": "test", "shell-version": ["46"]}))
    (ext / "extension.js").write_text(
        "export default class Probe { enable() {} disable() {} }\n")
    with Session(extensions=[ext]) as s:
        info = _wait_for(lambda: "ACTIVE" in (r := s.run(
            ["gnome-extensions", "info", uuid])).stdout and r.stdout)
        assert info and f"Path: {s.root}/data/gnome-shell/extensions/{uuid}" in info, info


@gnome
def test_boot_crash_fails_fast_with_log_tail():
    require_session_tools()
    s = Session(shell_args=["--no-such-option"])
    t0 = time.monotonic()
    with pytest.raises(SessionError, match="exited during boot") as e:
        s.start()
    assert time.monotonic() - t0 < 10
    assert "gnome-shell log tail" in str(e.value) and "no-such-option" in str(e.value)
    _assert_gone((s.root, s.runtime_dir), s.runtime_dir)


@gnome
def test_cli_runs_command_and_cleans_up():
    require_session_tools()
    r = subprocess.run([sys.executable, "-m", "tests.linux.session", "--",
                        "sh", "-c", 'echo "$WAYLAND_DISPLAY $XDG_RUNTIME_DIR"; exit 3'],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, r.stderr
    name, rt = r.stdout.split()
    assert name.startswith("dsr-") and rt.startswith("/tmp/dsr.")
    _assert_gone((rt,), rt)


@gnome
def test_cli_ctrl_c_cleans_up():
    require_session_tools()
    p = subprocess.Popen([sys.executable, "-m", "tests.linux.session"], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        exports = {}
        for line in p.stdout:
            k, _, v = line.removeprefix("export ").strip().partition("=")
            exports[k] = v
            if k == "HOME":
                break
        rt = exports["XDG_RUNTIME_DIR"]
        assert Path(rt).is_dir() and _pids_with_runtime_dir(rt)
        p.send_signal(signal.SIGINT)
        assert p.wait(timeout=20) == 130
    finally:
        if p.poll() is None:
            p.kill()
    _assert_gone((rt, str(Path(exports["HOME"]).parent)), rt)


# ------------------------------------------------------------- no shell needed
def test_reap_stale_sessions_only_touches_dead_owners(tmp_path):
    dead = tmp_path / "dsr.dead"
    alive = tmp_path / "dsr.alive"
    foreign = tmp_path / "dsr.foreign"   # not created by the launcher: no marker
    for d in (dead, alive, foreign):
        d.mkdir()
    dead_root = tmp_path / "dragshift-session-dead"
    dead_root.mkdir()
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    (dead / MARKER).write_text(json.dumps({"pid": gone.pid, "start": "0", "root": str(dead_root)}))
    from tests.linux.session import _pid_start
    (alive / MARKER).write_text(json.dumps({"pid": os.getpid(), "start": _pid_start(os.getpid())}))
    assert reap_stale_sessions(str(tmp_path)) == [str(dead)]
    assert not dead.exists() and not dead_root.exists()
    assert alive.exists() and foreign.exists()


def test_ubuntu_mode_needs_mode_file_and_theme(tmp_path, monkeypatch):
    mode = tmp_path / "modes" / "ubuntu.json"
    monkeypatch.setattr(session_mod, "UBUNTU_MODE", mode)
    assert not ubuntu_mode()                       # no mode file (other distros)
    mode.parent.mkdir()
    mode.write_text(json.dumps({"themeResourceName": "theme/Yaru/gnome-shell-theme.gresource"}))
    assert not ubuntu_mode()                       # mode file without the Yaru shell theme
    theme = tmp_path / "theme" / "Yaru" / "gnome-shell-theme.gresource"
    theme.parent.mkdir(parents=True)
    theme.write_bytes(b"")
    assert ubuntu_mode()
