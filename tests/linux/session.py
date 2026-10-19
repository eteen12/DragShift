"""Isolated headless GNOME Shell session for tests (Linux only).

Boots `gnome-shell --headless --wayland --virtual-monitor WxH` on a private D-Bus with a
throwaway HOME, XDG directories and XDG_RUNTIME_DIR, hands its environment to child
processes, and tears all of it down again. Nothing in the user's real desktop is used or
touched:

- The child environment is built from a short whitelist, so the real DISPLAY,
  WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, XAUTHORITY and SESSION_MANAGER never leak in.
- The session gets its own (empty) system bus by default, so the shell cannot register a
  geoclue agent, take logind inhibitors, set the lock hint of the real login session or
  start system services. `XDG_SESSION_ID` points at a session that does not exist, for
  the same reason when the real system bus is used.
- Ubuntu's desktop-icons extension (DING) kills every `gjs <its path>/app/ding.js` of the
  same user when it is enabled, which would kill the real desktop's icons. The session runs
  a private copy from its own XDG_DATA_HOME, so it only ever matches its own copy.
- Teardown kills the session's process groups and every process whose environment carries
  the session's unique XDG_RUNTIME_DIR (ibus-daemon and apps started via D-Bus leave the
  process group), then removes the temporary directories.

Usage::

    with Session() as s:
        s.run(["gnome-extensions", "list"])

    python -m tests.linux.session -- nautilus --new-window /tmp
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOOT_TIMEOUT = 30.0        # seconds until org.gnome.Shell must answer ShellVersion
KILL_GRACE = 5.0           # seconds between SIGTERM and SIGKILL on teardown
SUN_PATH_MAX = 107         # AF_UNIX path limit (108 bytes including the NUL)
RUNTIME_PREFIX = "dsr."    # mktemp -d /tmp/dsr.XXXX: keeps the Wayland socket path short
MARKER = "dragshift-session.json"
FAKE_SESSION_ID = "dragshift-isolated"

SYSTEM_EXTENSIONS = Path("/usr/share/gnome-shell/extensions")
# System extensions that must run from a private copy (see module docstring).
PRIVATE_COPY_EXTENSIONS = ("ding@rastersoft.com",)
UBUNTU_MODE = Path("/usr/share/gnome-shell/modes/ubuntu.json")

_PASSTHROUGH = ("PATH", "LANG", "LANGUAGE", "TZ", "TERM", "USER", "LOGNAME", "SHELL",
                "VIRTUAL_ENV", "PYTHONPATH")
_REQUIRED_TOOLS = ("gnome-shell", "dbus-run-session", "dbus-daemon", "gdbus",
                   "dbus-update-activation-environment")

_SYSTEM_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
  </policy>
</busconfig>
"""

# Seeded into the session's own dconf database before boot.
_DCONF_SEED = """[org/gnome/shell]
enabled-extensions={enabled}
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/system/location]
enabled=false
"""


class SessionError(RuntimeError):
    """The session could not be started (the message carries the shell log tail)."""


def missing_tools() -> list[str]:
    """Programs the launcher needs that are not available."""
    missing = [t for t in _REQUIRED_TOOLS if shutil.which(t) is None]
    if gi_python() is None:
        missing.append("python3 with gi (python3-gi)")
    return missing


def gi_python() -> str | None:
    """A system python3 that can import gi (runs fake_systemd.py)."""
    for py in dict.fromkeys(("/usr/bin/python3", shutil.which("python3") or "")):
        if py and os.access(py, os.X_OK) and subprocess.run(
                [py, "-I", "-c", "from gi.repository import Gio"],
                capture_output=True).returncode == 0:
            return py
    return None


def _pid_start(pid: int) -> str | None:
    """Kernel start time of a pid (field 22 of /proc/<pid>/stat), to detect pid reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def _pids_with_runtime_dir(runtime_dir: str) -> list[int]:
    """Our own processes whose environment has XDG_RUNTIME_DIR=<runtime_dir>.

    The runtime dir is unique per session (mkdtemp), so this never matches a process of
    the real desktop or of another session.
    """
    needle = f"XDG_RUNTIME_DIR={runtime_dir}".encode()
    uid, me, found = os.getuid(), os.getpid(), []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def _mounts_under(paths: list[str]) -> list[str]:
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return []
    points = [ln.split()[1].replace("\\040", " ") for ln in lines if len(ln.split()) > 1]
    return [p for p in points if any(p == d or p.startswith(d + "/") for d in paths)]


def _unmount(path: str) -> None:
    """Lazy-unmount a leftover FUSE mount (gvfs/document portal) before removal."""
    for tool in ("fusermount3", "fusermount"):
        if shutil.which(tool):
            subprocess.run([tool, "-u", "-z", path], capture_output=True)
            return


def _signal(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def reap_stale_sessions(tmp: str = "/tmp") -> list[str]:
    """Clean up sessions whose launcher process died without tearing down (SIGKILL, crash).

    Only directories this launcher created (they hold a MARKER naming the owning pid and
    its start time) are considered, and only when that exact process is gone. Returns the
    removed runtime dirs.
    """
    removed = []
    for rt in Path(tmp).glob(RUNTIME_PREFIX + "*"):
        try:
            if rt.stat().st_uid != os.getuid():
                continue
            info = json.loads((rt / MARKER).read_text())
        except (OSError, ValueError):
            continue
        if _pid_start(int(info["pid"])) == info.get("start"):
            continue  # owner still running
        pids = _pids_with_runtime_dir(str(rt))
        _signal(pids, signal.SIGKILL)
        for mp in _mounts_under([str(rt)]):
            _unmount(mp)
        root = info.get("root")
        if root and Path(root).name.startswith("dragshift-session-"):
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(rt, ignore_errors=True)
        removed.append(str(rt))
    return removed


_LIVE: set["Session"] = set()


@atexit.register
def _stop_all() -> None:
    for s in list(_LIVE):
        s.stop()


class Session:
    """One isolated GNOME Shell session. Use as a context manager or call start()/stop().

    extensions: paths of extension directories (named by UUID) to copy into the session
        and enable before boot.
    system_bus: "isolated" (default) gives the session an empty private system bus;
        "host" lets it use the real one (still detached from the real login session).
    keep: leave the temporary directories in place on stop() (for debugging).
    """

    def __init__(self, size: tuple[int, int] = (1600, 900), extensions=(),
                 timeout: float = BOOT_TIMEOUT, system_bus: str = "isolated",
                 shell_args=(), keep: bool = False):
        if system_bus not in ("isolated", "host"):
            raise ValueError("system_bus must be 'isolated' or 'host'")
        self.size = size
        self.extensions = [Path(e) for e in extensions]
        self.timeout = timeout
        self.system_bus = system_bus
        self.shell_args = list(shell_args)
        self.keep = keep
        self.root: Path | None = None
        self.runtime_dir: Path | None = None
        self.wayland_display: str | None = None
        self.display: str | None = None
        self.xauthority: str | None = None
        self.bus_address: str | None = None
        self.system_bus_address: str | None = None
        self.shell_version: str | None = None
        self.shell_pid: int | None = None
        self.log_path: Path | None = None
        self._procs: list[subprocess.Popen] = []
        self._log = None
        self._base: dict[str, str] = {}

    # ----------------------------------------------------------------- public API
    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def env(self, **extra: str) -> dict[str, str]:
        """Environment for a process that should run inside the session."""
        if not self.bus_address:
            raise SessionError("session is not running")
        env = dict(self._base)
        env.update(DBUS_SESSION_BUS_ADDRESS=self.bus_address,
                   WAYLAND_DISPLAY=self.wayland_display)
        if self.display:
            env["DISPLAY"] = self.display
        if self.xauthority:
            env["XAUTHORITY"] = self.xauthority
        env.update(extra)
        return env

    def run(self, cmd, **kw) -> subprocess.CompletedProcess:
        """subprocess.run inside the session (text output captured unless overridden)."""
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
        kw.setdefault("timeout", 60)
        return subprocess.run(cmd, env=self.env(**kw.pop("env", {})), **kw)

    def popen(self, cmd, **kw) -> subprocess.Popen:
        """subprocess.Popen inside the session; the process is killed on stop()."""
        return subprocess.Popen(cmd, env=self.env(**kw.pop("env", {})), **kw)

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except (OSError, AttributeError):
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def start(self) -> "Session":
        if self.root is not None:
            raise SessionError("session already started")
        missing = missing_tools()
        if missing:
            raise SessionError("missing programs: " + ", ".join(missing))
        reap_stale_sessions()
        _LIVE.add(self)
        try:
            self._prepare()
            self._boot()
        except BaseException:
            tail = self.log_tail()
            self.stop()
            if tail and sys.exc_info()[0] is SessionError:
                raise SessionError(f"{sys.exc_info()[1]}\n--- gnome-shell log tail ---\n{tail}")
            raise
        return self

    def stop(self) -> None:
        """Kill everything the session started and remove its directories. Idempotent."""
        _LIVE.discard(self)
        rt = str(self.runtime_dir) if self.runtime_dir else None
        groups = [p.pid for p in self._procs if p.poll() is None]
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if rt:
            _signal(_pids_with_runtime_dir(rt), signal.SIGTERM)
        deadline = time.monotonic() + KILL_GRACE
        while time.monotonic() < deadline:
            for p in self._procs:
                p.poll()
            left = [p for p in self._procs if p.returncode is None]
            if not left and not (rt and _pids_with_runtime_dir(rt)):
                break
            time.sleep(0.1)
        for _ in range(3):  # SIGKILL survivors, including anything spawned meanwhile
            for p in self._procs:
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            pids = _pids_with_runtime_dir(rt) if rt else []
            _signal(pids, signal.SIGKILL)
            for p in self._procs:
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if not pids:
                break
            time.sleep(0.2)
        self._procs.clear()
        if self._log:
            self._log.close()
            self._log = None
        dirs = [str(d) for d in (self.runtime_dir, self.root) if d]
        for mp in sorted(_mounts_under(dirs), key=len, reverse=True):
            _unmount(mp)
        if self.keep:  # kept for debugging: drop the marker so no later start reaps it
            if self.runtime_dir:
                (self.runtime_dir / MARKER).unlink(missing_ok=True)
        else:
            for d in dirs:
                if not _mounts_under([d]):  # never recurse into a live mount
                    shutil.rmtree(d, ignore_errors=True)
        self.bus_address = None

    # ----------------------------------------------------------------- internals
    def _prepare(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dragshift-session-"))
        self.runtime_dir = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX, dir="/tmp"))
        os.chmod(self.runtime_dir, 0o700)  # Mutter refuses a runtime dir that is not 0700
        (self.runtime_dir / MARKER).write_text(json.dumps(
            {"pid": os.getpid(), "start": _pid_start(os.getpid()), "root": str(self.root)}))
        self.wayland_display = f"dsr-{os.getpid()}-{secrets.token_hex(3)}"
        if self.wayland_display == os.environ.get("WAYLAND_DISPLAY"):
            raise SessionError("refusing to reuse the real WAYLAND_DISPLAY")
        if len(str(self.runtime_dir / self.wayland_display)) + len(".lock") > SUN_PATH_MAX:
            raise SessionError("Wayland socket path too long; use a shorter /tmp")
        self.log_path = self.root / "gnome-shell.log"

        dirs = {k: self.root / sub for k, sub in (
            ("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
            ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"))}
        for d in dirs.values():
            d.mkdir()
        ubuntu = UBUNTU_MODE.exists()
        env = {k: v for k, v in os.environ.items() if k in _PASSTHROUGH or k.startswith("LC_")}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.update({k: str(v) for k, v in dirs.items()})
        env.update(
            XDG_RUNTIME_DIR=str(self.runtime_dir),
            XDG_SESSION_TYPE="wayland",
            XDG_SESSION_ID=FAKE_SESSION_ID,
            XDG_CURRENT_DESKTOP="ubuntu:GNOME" if ubuntu else "GNOME",
            # <root>/share comes first so the private DING copy shadows the system one.
            XDG_DATA_DIRS=":".join([str(self.root / "share")] + [
                d for d in ("/usr/share/ubuntu", "/usr/share/gnome", "/usr/local/share",
                            "/usr/share") if Path(d).is_dir()]),
            XDG_CONFIG_DIRS=":".join(d for d in ("/etc/xdg/xdg-ubuntu", "/etc/xdg")
                                     if Path(d).is_dir()) or "/etc/xdg",
            GVFS_DISABLE_FUSE="1",
        )
        self._base = env

        # Private copies go to a *system* data dir of the session (not XDG_DATA_HOME):
        # Ubuntu's shell refuses per-user copies of session-mode extensions.
        private = self.root / "share" / "gnome-shell" / "extensions"
        private.mkdir(parents=True)
        for uuid in PRIVATE_COPY_EXTENSIONS:
            if (SYSTEM_EXTENSIONS / uuid).is_dir():
                shutil.copytree(SYSTEM_EXTENSIONS / uuid, private / uuid, symlinks=True)
        ext_dir = dirs["XDG_DATA_HOME"] / "gnome-shell" / "extensions"
        ext_dir.mkdir(parents=True)
        # Private-bus stand-in for systemd, so Xwayland can start (see fake_systemd.py).
        shutil.copy(Path(__file__).with_name("fake_systemd.py"), self.root / "fake_systemd.py")
        services = dirs["XDG_DATA_HOME"] / "dbus-1" / "services"
        services.mkdir(parents=True)
        (services / "org.freedesktop.systemd1.service").write_text(
            "[D-BUS Service]\nName=org.freedesktop.systemd1\n"
            f"Exec={gi_python()} -I {self.root / 'fake_systemd.py'}\n")
        for src in self.extensions:
            if not (src / "metadata.json").is_file():
                raise SessionError(f"not an extension directory: {src}")
            shutil.copytree(src, ext_dir / src.name, symlinks=True)
        self._seed_dconf(dirs["XDG_CONFIG_HOME"], [e.name for e in self.extensions])

    def _seed_dconf(self, config_home: Path, enabled: list[str]) -> None:
        keyfiles = self.root / "dconf-seed"
        keyfiles.mkdir()
        (keyfiles / "00-session").write_text(_DCONF_SEED.format(
            enabled="[" + ", ".join(f"'{u}'" for u in enabled) + "]" if enabled else "@as []"))
        (config_home / "dconf").mkdir()
        if shutil.which("dconf"):
            subprocess.run(["dconf", "compile", str(config_home / "dconf" / "user"),
                            str(keyfiles)], check=True, capture_output=True)
            self._post_boot_settings = []
        else:  # no dconf CLI (minimal images): apply through gsettings once the bus is up
            self._post_boot_settings = [
                ["org.gnome.shell", "enabled-extensions", repr(enabled) if enabled else "@as []"],
                ["org.gnome.desktop.session", "idle-delay", "0"],
                ["org.gnome.desktop.screensaver", "lock-enabled", "false"],
                ["org.gnome.desktop.lockdown", "disable-lock-screen", "true"],
            ]

    def _spawn(self, cmd, env) -> subprocess.Popen:
        p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=self._log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        self._procs.append(p)
        return p

    def _boot(self) -> None:
        self._log = open(self.log_path, "wb")
        env = dict(self._base)
        if self.system_bus == "isolated":
            sock = self.runtime_dir / "system_bus_socket"
            conf = self.root / "system-bus.conf"
            conf.write_text(_SYSTEM_BUS_CONFIG.format(socket=sock))
            self._spawn(["dbus-daemon", f"--config-file={conf}", "--nofork"], env)
            self.system_bus_address = f"unix:path={sock}"
            self._wait(lambda: sock.exists(), 5, "private system bus did not start")
            env["DBUS_SYSTEM_BUS_ADDRESS"] = self.system_bus_address
            self._base["DBUS_SYSTEM_BUS_ADDRESS"] = self.system_bus_address

        addr_file = self.root / "bus-address"
        # The bus daemon (and every service it activates) sees WAYLAND_DISPLAY from the
        # start; the shell itself must not, or it would not own the display.
        wrapper = ('printf %s "$DBUS_SESSION_BUS_ADDRESS" > "$1"; shift; '
                   'unset WAYLAND_DISPLAY; exec "$@"')
        w, h = self.size
        cmd = ["dbus-run-session", "--", "sh", "-c", wrapper, "sh", str(addr_file),
               "gnome-shell", "--headless", "--wayland",
               *(["--mode=ubuntu"] if UBUNTU_MODE.exists() else []),
               "--virtual-monitor", f"{w}x{h}", f"--wayland-display={self.wayland_display}",
               *self.shell_args]
        shell = self._spawn(cmd, dict(env, WAYLAND_DISPLAY=self.wayland_display))
        deadline = time.monotonic() + self.timeout

        def check_alive():
            if shell.poll() is not None:
                raise SessionError(f"gnome-shell exited during boot (status {shell.returncode})")

        self._wait(lambda: addr_file.exists() and addr_file.read_text().strip(),
                   deadline - time.monotonic(), "private session bus did not start", check_alive)
        self.bus_address = addr_file.read_text().strip()

        def version():
            check_alive()
            r = subprocess.run(
                ["gdbus", "call", "--session", "--timeout", "2", "--dest", "org.gnome.Shell",
                 "--object-path", "/org/gnome/Shell", "--method",
                 "org.freedesktop.DBus.Properties.Get", "org.gnome.Shell", "ShellVersion"],
                env=self.env(), capture_output=True, text=True)
            m = re.search(r"'([^']+)'", r.stdout) if r.returncode == 0 else None
            return m.group(1) if m else None

        self.shell_version = self._wait(version, deadline - time.monotonic(),
                                        f"org.gnome.Shell did not answer within {self.timeout:.0f} s")
        self.shell_pid = self._find_shell_pid(shell.pid)
        self.display = self._wait(self._find_display, max(5.0, deadline - time.monotonic()),
                                  "Xwayland display not found", check_alive)
        auth = sorted(self.runtime_dir.glob(".mutter-Xwaylandauth.*"))
        self.xauthority = str(auth[0]) if auth else None
        for args in getattr(self, "_post_boot_settings", []):
            self.run(["gsettings", "set", *args], check=True)
        pairs = [f"WAYLAND_DISPLAY={self.wayland_display}", f"DISPLAY={self.display}",
                 "XDG_SESSION_TYPE=wayland"]
        if self.xauthority:
            pairs.append(f"XAUTHORITY={self.xauthority}")
        r = self.run(["dbus-update-activation-environment", *pairs])
        if r.returncode != 0:
            raise SessionError(f"dbus-update-activation-environment failed: {r.stderr.strip()}")

    def _find_shell_pid(self, launcher_pid: int) -> int | None:
        """The gnome-shell process under dbus-run-session (the sh wrapper exec()s into it)."""
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            if ppid == launcher_pid and comm == "gnome-shell":
                return int(entry.name)
        return None

    def _find_display(self) -> str | None:
        """Mutter's public X11 display: the lowest /tmp/.X<n>-lock owned by our shell pid,
        or the number Mutter logs. Stale sockets of other sessions are ignored."""
        if self.shell_pid:
            mine = []
            for lock in Path("/tmp").glob(".X*-lock"):
                try:
                    if int(lock.read_text().strip()) == self.shell_pid:
                        mine.append(int(lock.name[2:-5]))
                except (OSError, ValueError):
                    continue
            if mine:
                return f":{min(mine)}"
        m = re.search(r"Using public X11 display :(\d+)",
                      self.log_path.read_text(errors="replace"))
        return f":{m.group(1)}" if m else None

    @staticmethod
    def _wait(fn, timeout, what, check=None):
        deadline = time.monotonic() + timeout
        while True:
            if check:
                check()
            value = fn()
            if value:
                return value
            if time.monotonic() > deadline:
                raise SessionError(what)
            time.sleep(0.1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tests.linux.session",
        description="Run a command inside an isolated headless GNOME Shell session. "
                    "Without a command, print the session environment and wait for Ctrl-C.")
    ap.add_argument("--size", default="1600x900", help="virtual monitor size (WxH)")
    ap.add_argument("--extension", action="append", default=[], metavar="DIR",
                    help="extension directory (named by UUID) to install and enable")
    ap.add_argument("--host-system-bus", action="store_true",
                    help="let the shell use the real system bus instead of a private one")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directories")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to run")
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    w, h = (int(v) for v in a.size.lower().split("x"))

    def _terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    t0 = time.monotonic()
    try:
        with Session((w, h), extensions=a.extension, keep=a.keep,
                     system_bus="host" if a.host_system_bus else "isolated") as s:
            print(f"GNOME Shell {s.shell_version} up in {time.monotonic() - t0:.1f} s: "
                  f"WAYLAND_DISPLAY={s.wayland_display} DISPLAY={s.display} "
                  f"log={s.log_path}", file=sys.stderr, flush=True)
            if cmd:
                return subprocess.run(cmd, env=s.env()).returncode
            for k in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY",
                      "XDG_RUNTIME_DIR", "HOME"):
                print(f"export {k}={s.env().get(k, '')}", flush=True)
            print("# Ctrl-C to stop", file=sys.stderr, flush=True)
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        return 130
    except SessionError as e:
        print(f"session failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
