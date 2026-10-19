"""Linux / GNOME integration (only imported on Linux).

This is the single place the app asks for anything Linux-specific. GNOME on Wayland gives
normal clients no global pointer state and no screen capture, so most of it will come
from DragShift's GNOME Shell extension over D-Bus; the rest is plain XDG desktop files.

Every function here is a safe fallback for now (None / False / no-op) and gets its
real body in a later step; callers already treat the fallback as "not available" and keep
their existing behaviour. Keep this module importable without D-Bus/GObject libraries:
import them lazily inside the functions that need them.
"""
from __future__ import annotations

from pathlib import Path

# Clutter.ModifierType bits, as the shell extension reports them in `modifier_mask`
SHIFT_MASK, CONTROL_MASK, MOD1_MASK = 1 << 0, 1 << 2, 1 << 3
BUTTON1_MASK = 1 << 8
KEY_MASKS = {"shift": SHIFT_MASK, "ctrl": CONTROL_MASK, "alt": MOD1_MASK}


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

