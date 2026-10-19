"""Instant hand-off: if DragShift is already running, pass files to it and exit *before*
importing Qt. Right-click → DragShift / Send To then opens the menu in milliseconds."""
from __future__ import annotations

import json
import os
import sys
import time

# Linux: how long a start handed to the shell extension may take until the new instance
# takes the hand-off (Python + Qt start-up; seconds at login on a slow disk).
MANAGED_START_TIMEOUT = 20.0


def ipc_name() -> str:
    return "DragShift-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


def _files(argv: list[str]) -> list[str]:
    return [os.path.abspath(a) for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]


def _send(payload: dict) -> bool:
    """Write `payload` to the running instance's IPC server; False when none listens."""
    data = json.dumps(payload).encode("utf-8")
    try:
        if sys.platform == "win32":
            path = "\\\\.\\pipe\\" + ipc_name()       # QLocalServer's named pipe: \\.\pipe\<name>
            with open(path, "wb", buffering=0) as f:
                f.write(data)
                time.sleep(0.15)  # let the running copy read before the pipe closes
            return True
        import socket
        import tempfile
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(os.path.join(tempfile.gettempdir(), ipc_name()))
        s.sendall(data)
        s.close()
        return True
    except OSError:
        return False


def try_handoff(argv: list[str]) -> bool:
    if "--selftest" in argv:
        return False
    files = _files(argv)
    return _send({"files": files} if files else {"show": "--quiet" not in argv})


def start_managed(argv: list[str]) -> bool:
    """Linux, when no instance took the hand-off: have DragShift's GNOME Shell extension
    start the app as the shell's own Wayland client, so its menu and toasts stay out of
    the dock and Alt+Tab (#95). Hands this launch's files to the new instance once it
    listens, then True: this process exits. False (run here, unmanaged, as before) when
    this process was itself started that way, without the extension or its launcher, or
    when the new instance does not answer within MANAGED_START_TIMEOUT. The XDG autostart
    entry, the app grid and the command line all come through here, so the extension
    never starts a second copy."""
    from . import platform_linux
    if os.environ.pop(platform_linux.MANAGED_ENV, None) is not None or "--selftest" in argv:
        return False
    files = _files(argv)
    # The new instance shows the start-up toast or welcome itself unless quiet; with files
    # it opens the menu for them instead, like a cold start with files.
    if not platform_linux.launch_app(quiet="--quiet" in argv or bool(files)):
        return False
    payload = {"files": files} if files else {"show": False}
    deadline = time.monotonic() + MANAGED_START_TIMEOUT
    while time.monotonic() < deadline:
        if _send(payload):
            return True
        time.sleep(0.05)
    print("DragShift: the instance the GNOME Shell extension started did not answer; "
          "starting here instead", file=sys.stderr, flush=True)
    return False
