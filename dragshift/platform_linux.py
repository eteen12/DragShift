"""Linux / GNOME integration (only imported on Linux).

This is the single place the app asks for anything Linux-specific. GNOME on Wayland gives
normal clients no global pointer state and no screen capture, so most of it will come
from DragShift's GNOME Shell extension over D-Bus; the rest is plain XDG desktop files.

Functions without a real body yet are safe fallbacks (None / False / no-op); callers
already treat the fallback as "not available" and keep their existing behaviour. Keep this
module importable without D-Bus/GObject libraries: import them lazily inside the functions
that need them (the D-Bus client is `jeepney`, a Linux-only requirement).
"""
from __future__ import annotations

import threading
from pathlib import Path

# Clutter.ModifierType bits, as the shell extension reports them in `modifier_mask`
SHIFT_MASK, CONTROL_MASK, MOD1_MASK = 1 << 0, 1 << 2, 1 << 3
BUTTON1_MASK = 1 << 8
KEY_MASKS = {"shift": SHIFT_MASK, "ctrl": CONTROL_MASK, "alt": MOD1_MASK}


# ---------------------------------------------------------------- shell extension D-Bus
# The extension (extension/dragshift@eteen12.github.io, interface in its dbus.xml) serves
# this on the session bus.
EXTENSION_UUID = "dragshift@eteen12.github.io"
BUS_NAME = "io.github.eteen12.DragShift"
OBJECT_PATH = "/io/github/eteen12/DragShift"
INTERFACE = "io.github.eteen12.DragShift"
DBUS_TIMEOUT = 0.2   # seconds; a missing extension answers at once (ServiceUnknown)

_bus = None
_bus_lock = threading.Lock()


def _call(method: str, signature: str | None = None, *args, timeout: float = DBUS_TIMEOUT):
    """Call `method` on the extension and return the reply's arguments as a tuple, or None
    when it can't answer (no session bus, extension missing or disabled, error, timeout).
    Never raises. One connection is kept open and reopened after a failure."""
    global _bus
    try:
        from jeepney import DBusAddress, MessageType, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return None
    with _bus_lock:
        try:
            if _bus is None:
                _bus = open_dbus_connection(bus="SESSION", auth_timeout=timeout)
            msg = new_method_call(DBusAddress(OBJECT_PATH, BUS_NAME, INTERFACE),
                                  method, signature, args)
            reply = _bus.send_and_get_reply(msg, timeout=timeout)
        except Exception:
            if _bus is not None:
                try:
                    _bus.close()
                except Exception:
                    pass
                _bus = None
            return None
    if reply.header.message_type != MessageType.method_return:
        return None
    return reply.body


def extension_version() -> str | None:
    """The DragShift version the running shell extension was built from, or None when the
    extension does not answer."""
    body = _call("Version")
    return body[0] if body and isinstance(body[0], str) else None


# ---------------------------------------------------------------- input state
def pointer_state() -> tuple[int, int, bool, int] | None:
    """Global pointer `(x, y, left_down, modifier_mask)` in logical screen pixels, or None
    when unknown (extension missing). Never derived from QCursor.pos(): under XWayland it
    is stale while the pointer is over Wayland windows."""
    return None


def extension_available() -> bool:
    """True when DragShift's GNOME Shell extension answers on the session bus."""
    return False


# ---------------------------------------------------------------- screen capture
def capture(x: int, y: int, w: int, h: int, exclude_self: bool = False):
    """Screen rect (logical px) as a PIL RGB image, or None. The drag picture (Mutter's
    drag actor) is ALWAYS left out. exclude_self additionally leaves DragShift's own
    windows out, for live refresh behind toasts and tool windows."""
    return None


def wallpaper_crop(x: int, y: int, w: int, h: int):
    """The desktop wallpaper under a screen rect as a PIL RGB image of size w×h, or None."""
    return None


# ---------------------------------------------------------------- desktop integration
def autostart_enabled() -> bool:
    return False


def set_autostart(on: bool) -> None:
    return None


def reveal(path: str | Path) -> bool:
    """Show `path` selected in the file manager. False = not handled, caller falls back."""
    return False

