"""Instant hand-off: if DragShift is already running, pass files to it and exit *before*
importing Qt. Right-click → DragShift / Send To / Open With then opens the menu in
milliseconds.

Where the running app listens (the app's QLocalServer and this client use the same name):
- Windows: the named pipe \\\\.\\pipe\\DragShift-<user>.
- macOS: the socket <tempdir>/DragShift-<user> (Qt puts a plain name in the temp dir).
- Linux: the socket $XDG_RUNTIME_DIR/DragShift, given to Qt as an absolute path. The
  runtime dir belongs to the user alone, so another local user cannot take the name first,
  as one can in /tmp. Without a private runtime dir it falls back to the macOS rule."""
from __future__ import annotations

import json
import os
import sys

LINUX_SOCKET = "DragShift"


def _user_name(env) -> str:
    return "DragShift-" + (env.get("USERNAME") or env.get("USER") or "user")


def _private_dir(path: str) -> bool:
    """XDG base-dir spec: the runtime dir is owned by the user and has mode 0700."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    import stat
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def ipc_name(env=None) -> str:
    """The name the app's QLocalServer listens on: a plain name, or on Linux an absolute
    socket path in $XDG_RUNTIME_DIR."""
    env = os.environ if env is None else env
    if sys.platform.startswith("linux"):
        run = env.get("XDG_RUNTIME_DIR", "")
        if os.path.isabs(run) and _private_dir(run):
            return os.path.join(run, LINUX_SOCKET)
    return _user_name(env)


def socket_path(env=None) -> str:
    """Where the unix socket is (not Windows): ipc_name() if it is a path, else where Qt
    puts a plain name, the temp dir."""
    name = ipc_name(env)
    if os.path.isabs(name):
        return name
    import tempfile
    return os.path.join(tempfile.gettempdir(), name)


def try_handoff(argv: list[str]) -> bool:
    if "--selftest" in argv:
        return False
    files = [os.path.abspath(a) for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    payload = {"files": files} if files else {"show": "--quiet" not in argv}
    data = json.dumps(payload).encode("utf-8")
    try:
        if sys.platform == "win32":
            path = "\\\\.\\pipe\\" + ipc_name()       # QLocalServer's named pipe: \\.\pipe\<name>
            with open(path, "wb", buffering=0) as f:
                f.write(data)
                import time
                time.sleep(0.15)  # let the running copy read before the pipe closes
            return True
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(socket_path())
        s.sendall(data)
        s.close()
        return True
    except OSError:
        return False
