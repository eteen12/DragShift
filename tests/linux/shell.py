"""Client for the test-helper extension `dragshift-test@eteen12.github.io` (Linux only).

The extension (tests/linux/ext/) runs inside the isolated session from session.py and
answers on `io.github.eteen12.DragShiftTest`. It is test-only: `Session(helper=True)`
copies it into the session's temporary XDG_DATA_HOME; nothing installs it anywhere else.

The project venv cannot import `gi`, so ShellHelper starts one bridge process per client:
the system python3 that has python3-gi (`session.gi_python()`), run inside the session's
environment, which makes the D-Bus calls with Gio and exchanges JSON lines with us. A call
costs about a millisecond instead of a process start.

Usage::

    with Session(helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        png = shell.screenshot()                       # Path under the session root
        win = shell.wait_for_window(wm_class="org.gnome.Nautilus")
        shell.activate_window(win["id"])
        shell.move_window(win["id"], 90, 150, 760, 560)  # frame x, y, w, h
        shell.move_pointer(500, 400)                   # input: see tests/linux/inject.py

This file is also the bridge (`python3 -I shell.py --bridge`), so the module level must
only use the standard library.
"""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
import time
from pathlib import Path

UUID = "dragshift-test@eteen12.github.io"
EXTENSION_DIR = Path(__file__).resolve().with_name("ext") / UUID
BUS_NAME = "io.github.eteen12.DragShiftTest"
OBJECT_PATH = "/io/github/eteen12/DragShiftTest"
INTERFACE = "io.github.eteen12.DragShiftTest"
READY_TIMEOUT = 30.0  # seconds for wait_ready (the extension normally answers within 1 s)
# org.gnome.Shell.Extensions extension states (js/misc/extensionUtils.js) that never
# become ready by themselves: ERROR, OUT_OF_DATE.
FAILED_STATES = {3, 4}

# D-Bus input signature of every method the client calls.
SIGNATURES = {
    "Version": "",
    "Screenshot": "(s)",
    "ScreenshotArea": "(iiiis)",
    "ListWindows": "",
    "RunningApps": "",
    "ActivateWindow": "(t)",
    "MoveWindow": "(tiiii)",
    "GetPointer": "",
    "GetWorkArea": "(ii)",
    "MovePointer": "(ii)",
    "Button": "(ub)",
    "Key": "(ub)",
    "TypeText": "(s)",
    "ReleaseAll": "",
}


class HelperError(RuntimeError):
    """A helper call failed (D-Bus error, bridge died, or the extension never answered)."""


class ShellHelper:
    """Calls the test-helper extension in a running `Session`."""

    def __init__(self, session, timeout: float = 10.0):
        from tests.linux.session import gi_python

        self.session = session
        self.timeout = timeout
        self._shots = itertools.count(1)
        self._bridge = session.popen(
            [gi_python(), "-I", str(Path(__file__).resolve()), "--bridge"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)

    def __enter__(self) -> "ShellHelper":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._bridge.poll() is None:
            self._bridge.stdin.close()
            try:
                self._bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._bridge.kill()
                self._bridge.wait()
        for f in (self._bridge.stdout, self._bridge.stderr):
            f.close()

    # ------------------------------------------------------------------ raw call
    def call(self, method: str, *args, timeout: float | None = None) -> list:
        """Call a helper method; returns its out arguments as a list."""
        return self._request({"method": method, "signature": SIGNATURES[method],
                              "args": list(args),
                              "timeout_ms": int((timeout or self.timeout) * 1000)})

    def _request(self, request: dict) -> list:
        method = request.get("method") or request.get("op")
        try:
            self._bridge.stdin.write(json.dumps(request) + "\n")
            self._bridge.stdin.flush()
            line = self._bridge.stdout.readline()
        except (BrokenPipeError, ValueError):
            line = ""
        if not line:
            self._bridge.wait(timeout=5)
            raise HelperError(f"D-Bus bridge exited ({self._bridge.returncode}): "
                              f"{self._bridge.stderr.read().strip()}")
        reply = json.loads(line)
        if "error" in reply:
            raise HelperError(f"{method}: {reply['error']}")
        return reply["result"]

    # ------------------------------------------------------------------ methods
    def version(self) -> str:
        return self.call("Version")[0]

    def wait_for_name(self, timeout: float) -> bool:
        """Whether the helper's bus name is owned within `timeout` s. Event-driven: the
        bridge watches the name (NameOwnerChanged) instead of polling."""
        return bool(self._request({"op": "wait_name", "name": BUS_NAME,
                                   "timeout_ms": int(timeout * 1000)})[0])

    def extension_state(self) -> int | None:
        """The shell's state for the helper extension (1 enabled, 3 error, 4 out of date,
        6 initialized, ...), or None if the shell does not know it (yet)."""
        try:
            info = self._request({
                "dest": "org.gnome.Shell", "path": "/org/gnome/Shell",
                "interface": "org.gnome.Shell.Extensions", "method": "GetExtensionInfo",
                "signature": "(s)", "args": [UUID], "timeout_ms": 5000})[0]
        except HelperError:
            return None
        return int(info["state"]) if "state" in info else None

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> str:
        """Wait until the extension owns its bus name and answers Version. It takes the
        name only once the shell's startup is done and the overview is closed (#17).
        Fails early when the extension is in ERROR state; the error carries the
        extension's state and the shell log tail (a syntax error shows up there)."""
        deadline = time.monotonic() + timeout
        last: Exception | str = f"{BUS_NAME} never appeared on the session bus"
        while True:
            try:
                if self.wait_for_name(max(0.1, min(1.0, deadline - time.monotonic()))):
                    return self.call("Version", timeout=5)[0]
            except HelperError as e:
                last = e
            if (self._bridge.poll() is not None or time.monotonic() > deadline
                    or self.extension_state() in FAILED_STATES):
                break
        info = self.session.run(["gnome-extensions", "info", UUID]).stdout.strip()
        try:  # the extension's own errors may already have scrolled out of the tail
            log = self.session.log_path.read_text(errors="replace").splitlines()
        except OSError:
            log = []
        mine = "\n".join([ln for ln in log if UUID in ln and "requested by" not in ln][-20:])
        raise HelperError(
            f"test helper not ready after {timeout:.0f} s: {last}\n"
            f"--- gnome-extensions info ---\n{info or '(not installed)'}\n"
            f"--- gnome-shell log lines naming {UUID} ---\n{mine or '(none)'}\n"
            f"--- gnome-shell log tail ---\n{self.session.log_tail()}")

    def _shot_path(self, path) -> Path:
        if path is None:
            path = self.session.root / "tmp" / f"screenshot-{next(self._shots)}.png"
        return Path(path)

    def screenshot(self, path=None) -> Path:
        """PNG of the whole stage without the cursor. `path` must lie inside the session
        root (default: <root>/tmp/screenshot-N.png)."""
        path = self._shot_path(path)
        self.call("Screenshot", str(path))
        return path

    def screenshot_area(self, x: int, y: int, width: int, height: int, path=None) -> Path:
        path = self._shot_path(path)
        self.call("ScreenshotArea", x, y, width, height, str(path))
        return path

    def list_windows(self) -> list[dict]:
        """Windows bottom to top. Keys: id, title, wm_class, pid, xid (0 unless X11),
        client ('x11'|'wayland'), frame (x, y, w, h), above, focused, minimized,
        stacking, window_type ('normal', 'desktop', ...), app (the shell's app id, e.g.
        'org.gnome.Nautilus.desktop', or 'window:N' when no desktop file matches),
        in_tab_list (Alt+Tab lists it), skip_taskbar."""
        windows = self.call("ListWindows")[0]
        for w in windows:
            w["frame"] = tuple(w["frame"])
        return windows

    def running_apps(self) -> list[str]:
        """Ids of the apps GNOME Shell counts as running (Shell.AppSystem.get_running(),
        what the dock marks with a dot), e.g. 'org.gnome.Nautilus.desktop'."""
        return self.call("RunningApps")[0]

    def wait_for_window(self, timeout: float = 15.0, **match) -> dict:
        """The topmost window whose fields equal every keyword (e.g. wm_class=...)."""
        deadline = time.monotonic() + timeout
        while True:
            windows = self.list_windows()
            found = [w for w in windows if all(w.get(k) == v for k, v in match.items())]
            if found:
                return found[-1]
            if time.monotonic() > deadline:
                raise HelperError(f"no window matching {match} after {timeout:.0f} s; "
                                  f"windows: {[(w['wm_class'], w['title']) for w in windows]}")
            time.sleep(0.1)

    def activate_window(self, window_id: int) -> bool:
        """Activate by ListWindows `id` or X11 window id; False if no such window."""
        return self.call("ActivateWindow", window_id)[0]

    def move_window(self, window_id: int, x: int, y: int, width: int, height: int,
                    timeout: float = 5.0) -> tuple[int, int, int, int]:
        """Put a window's frame at (x, y, width, height) (unmaximizing it first) and wait
        until ListWindows reports that frame and it stays put. Returns the final frame;
        Mutter may constrain it (minimum size, title bar on screen), in which case this
        returns what the window settled at after `timeout`."""
        if not self.call("MoveWindow", window_id, x, y, width, height)[0]:
            raise HelperError(f"MoveWindow: no window with id {window_id}")
        want = (x, y, width, height)
        deadline = time.monotonic() + timeout
        last, stable = None, 0
        while True:
            frame = next((w["frame"] for w in self.list_windows() if w["id"] == window_id),
                         None)
            if frame is None:
                raise HelperError(f"window {window_id} disappeared while moving")
            stable = stable + 1 if frame == last else 0
            last = frame
            if (frame == want and stable >= 2) or time.monotonic() > deadline:
                return frame
            time.sleep(0.05)

    def work_area(self, x: int, y: int) -> tuple[tuple, tuple]:
        """(monitor, work area) as (x, y, w, h) each, for the monitor that contains
        (x, y) (else the primary one), from Mutter's active workspace."""
        monitor, area = self.call("GetWorkArea", int(x), int(y))
        return tuple(monitor), tuple(area)

    def get_pointer(self) -> tuple[int, int, int]:
        """(x, y, Clutter modifier mask) from global.get_pointer()."""
        return tuple(self.call("GetPointer"))

    # Input injection (Clutter virtual devices inside the isolated shell only; the
    # extension refuses them anywhere else). tests/linux/inject.py builds on these.
    def move_pointer(self, x: int, y: int) -> tuple[int, int]:
        """Absolute move in stage coordinates. Returns once the shell's pointer is there
        (Mutter clamps it to the monitor), with the position it ended up at."""
        return tuple(self.call("MovePointer", int(round(x)), int(round(y))))

    def button(self, button: int, pressed: bool) -> None:
        """button: 1 primary, 2 middle, 3 secondary."""
        self.call("Button", button, pressed)

    def key(self, keyval: int, pressed: bool) -> None:
        """keyval: an X keysym (inject.KEYSYMS). Held until released explicitly."""
        self.call("Key", keyval, pressed)

    def type_text(self, text: str) -> None:
        """Press and release a key per character (keysyms as Gdk.unicode_to_keyval)."""
        self.call("TypeText", text)

    def release_all(self) -> None:
        """Release every key and button still held through the helper."""
        self.call("ReleaseAll")


def _wait_name(bus, name: str, timeout_ms: int) -> str:
    """The owner of `name` once it is owned, or "" after timeout_ms (bridge side)."""
    from gi.repository import Gio, GLib

    loop, owner = GLib.MainLoop(), []

    def appeared(_conn, _name, name_owner):
        owner.append(name_owner)
        loop.quit()

    def expired():
        loop.quit()
        timer.clear()
        return GLib.SOURCE_REMOVE

    watch = Gio.bus_watch_name_on_connection(bus, name, Gio.BusNameWatcherFlags.NONE,
                                             appeared, None)
    timer = [GLib.timeout_add(timeout_ms, expired)]
    if not owner:
        loop.run()
    Gio.bus_unwatch_name(watch)
    if timer:
        GLib.source_remove(timer[0])
    return owner[0] if owner else ""


def _bridge() -> int:
    """JSON-lines bridge to the helper over the session bus (system python with gi)."""
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    for line in sys.stdin:
        req = json.loads(line)
        try:
            if req.get("op") == "wait_name":
                out = {"result": [_wait_name(bus, req["name"], req["timeout_ms"])]}
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
                continue
            params = (GLib.Variant(req["signature"], tuple(req["args"]))
                      if req["signature"] else None)
            reply = bus.call_sync(req.get("dest", BUS_NAME), req.get("path", OBJECT_PATH),
                                  req.get("interface", INTERFACE), req["method"], params,
                                  None, Gio.DBusCallFlags.NONE, req["timeout_ms"], None)
            out = {"result": list(reply.unpack())}
        except GLib.Error as e:
            out = {"error": e.message}
        except (TypeError, ValueError, KeyError) as e:
            out = {"error": f"bad request: {e}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--bridge"]:
    sys.exit(_bridge())
