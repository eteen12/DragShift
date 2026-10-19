"""The widgets of a window over AT-SPI in the isolated GNOME session (Linux only).

Tool drivers (demo/e2e_linux.py) use it to find a button of a DragShift glass window by its
label and click it with injected input, the way e2e_windows.py finds controls through UI
Automation:

    w = Widgets(session)
    w.enable()                                   # once: Qt only exports its tree when asked
    for it in w.find("Compress — DragShift"):    # [{name, role, box, checked, enabled}, ...]
        ...

- Qt exports its accessible tree on the session's accessibility bus only while
  `org.a11y.Status.IsEnabled` (or `ScreenReaderEnabled`) is true; it watches the property,
  so `enable()` works on an app that is already running. GTK apps (Files) export theirs
  regardless. The launcher's session has its own accessibility bus (org.a11y.Bus is
  activated on the private session bus), so this never reaches the real desktop.
- `box` is in WINDOW coordinates: a Wayland client does not know where its window is, so
  SCREEN coordinates are meaningless. Add the frame's top-left from the helper's
  ListWindows (tests/linux/files.py does the same for Nautilus).
- Only widgets that are showing are returned (Qt also exports hidden ones).
- Lookups run a one-shot bridge process (the system python3 with python3-gi, `gi_python()`)
  in the session environment, so the module level only uses the standard library.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BRIDGE_TIMEOUT = 30.0      # s for one bridge process
ATSPI_TIMEOUT_MS = 3000    # per AT-SPI call, so a busy app can't hang a lookup for 25 s


class WidgetsError(RuntimeError):
    """No accessibility bus, no such window, or the bridge failed."""


class Widgets:
    """AT-SPI lookups in a running `Session` (tests.linux.session)."""

    def __init__(self, session, bridge_env: dict | None = None):
        self.session = session
        self.bridge_env = dict(bridge_env or {})

    def _bridge(self, request: dict, timeout: float = BRIDGE_TIMEOUT) -> dict:
        from tests.linux.session import gi_python
        py = gi_python()
        if py is None:
            raise WidgetsError("no python3 with python3-gi for the AT-SPI bridge")
        try:
            r = self.session.run([py, "-I", str(Path(__file__).resolve()), "--bridge",
                                  json.dumps(request)], env=self.bridge_env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise WidgetsError(f"AT-SPI bridge timed out after {timeout:.0f} s") from None
        try:
            reply = json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            # libatspi aborts the process when it cannot reach the bus.
            raise WidgetsError(f"AT-SPI bridge exited {r.returncode}: "
                               f"{r.stderr.strip()[-500:]}") from None
        if "error" in reply:
            raise WidgetsError(reply["error"])
        return reply

    def enable(self) -> None:
        """Turn on toolkit accessibility in the session (org.a11y.Status.IsEnabled)."""
        self._bridge({"cmd": "enable"})

    def available(self) -> bool:
        try:
            self._bridge({"cmd": "ping"})
            return True
        except WidgetsError:
            return False

    def find(self, title: str) -> list[dict]:
        """Every showing widget of the top-level window titled `title`, in tree order:
        {name, role, box: [x, y, w, h] (window coordinates), checked, enabled, focused}.
        Raises WidgetsError when no application exports a window with that title."""
        return self._bridge({"cmd": "find", "title": title})["widgets"]

    def press(self, title: str, name: str, role: str = "push button") -> None:
        """The AT-SPI action of a widget (a button's click) without any input; for cleanup
        only: drivers click with injected input."""
        self._bridge({"cmd": "press", "title": title, "name": name, "role": role})


def match(widgets: list[dict], name: str, role: str | None = None) -> list[dict]:
    """The widgets named exactly `name` (and of `role`), in tree order."""
    return [w for w in widgets if w["name"] == name and (role is None or w["role"] == role)]


# ---------------------------------------------------------------------- bridge
# Runs as `python3 -I widgets.py --bridge <json>` with the system python3 (python3-gi)
# inside the session environment; prints one JSON line.

def _children(node):
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            yield child


def _states(node, Atspi) -> set:
    s = node.get_state_set()
    return {name for name, st in (("showing", Atspi.StateType.SHOWING),
                                  ("visible", Atspi.StateType.VISIBLE),
                                  ("checked", Atspi.StateType.CHECKED),
                                  ("enabled", Atspi.StateType.ENABLED),
                                  ("focused", Atspi.StateType.FOCUSED)) if s.contains(st)}


def _walk(frame, Atspi) -> list[dict]:
    """Showing widgets under `frame` (the frame excluded), depth first in tree order."""
    out = []

    def walk(node):
        for child in _children(node):
            st = _states(child, Atspi)
            if "showing" not in st or "visible" not in st:
                continue                     # a hidden widget hides its children too
            e = child.get_extents(Atspi.CoordType.WINDOW)
            out.append({"name": child.get_name() or "", "role": child.get_role_name(),
                        "box": [e.x, e.y, e.width, e.height], "checked": "checked" in st,
                        "enabled": "enabled" in st, "focused": "focused" in st})
            walk(child)

    walk(frame)
    return out


def _frame(Atspi, title: str):
    desktop = Atspi.get_desktop(0)
    titles = []
    for app in _children(desktop):
        try:
            frames = list(_children(app))
        except Exception:        # an app that went away or doesn't answer
            continue
        for frame in frames:
            name = frame.get_name() or ""
            titles.append(name)
            if name == title:
                return frame, titles
    return None, titles


def _a11y_status(enable: bool):
    """Read (or set) org.a11y.Status.IsEnabled on the session bus; error text or None."""
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if enable:
            bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties",
                          "Set", GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled",
                                                        GLib.Variant("b", True))),
                          None, Gio.DBusCallFlags.NONE, 3000, None)
        bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                      GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as e:
        return f"no accessibility bus ({e.message})"
    return None


def _bridge(req) -> dict:
    cmd = req["cmd"]
    err = _a11y_status(cmd == "enable")
    if err:
        return {"error": err}
    if cmd in ("ping", "enable"):
        return {}
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    Atspi.set_timeout(ATSPI_TIMEOUT_MS, ATSPI_TIMEOUT_MS)
    frame, titles = _frame(Atspi, req["title"])
    if frame is None:
        return {"error": f"no accessible window titled {req['title']!r} (windows: {titles})"}
    if cmd == "find":
        return {"widgets": _walk(frame, Atspi)}
    if cmd == "press":
        stack = [frame]
        while stack:
            node = stack.pop(0)
            if node.get_name() == req["name"] and node.get_role_name() == req["role"]:
                node.do_action(0)
                return {}
            stack.extend(_children(node))
        return {"error": f"no {req['role']} named {req['name']!r} in {req['title']!r}"}
    return {"error": f"unknown command {cmd!r}"}


if __name__ == "__main__" and sys.argv[1:2] == ["--bridge"]:
    print(json.dumps(_bridge(json.loads(sys.argv[2]))), flush=True)
