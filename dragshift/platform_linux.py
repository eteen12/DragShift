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

import os
import sys
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


# ---------------------------------------------------------------- start at login
def autostart_path() -> Path:
    """$XDG_CONFIG_HOME/autostart/dragshift.desktop (an unset or relative value means ~/.config)."""
    conf = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(conf) if os.path.isabs(conf) else Path.home() / ".config"
    return base / "autostart" / "dragshift.desktop"


def launch_args() -> list[str]:
    """How to start DragShift quietly: the installed launcher, else this Python."""
    launcher = Path.home() / ".local" / "bin" / "dragshift"
    if launcher.is_file():
        return [str(launcher), "--quiet"]
    return [sys.executable, "-m", "dragshift", "--quiet"]


def _escape(value: str) -> str:
    """A desktop-file string value: backslash, newline, tab and CR escaped."""
    return (value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
            .replace("\r", "\\r"))


def _exec_quote(arg: str) -> str:
    """One Exec argument per the Desktop Entry spec: `%` doubled, an argument with reserved
    characters put in double quotes (with " ` $ \\ escaped), then the string escapes."""
    arg = arg.replace("%", "%%")
    if any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`"):
        arg = '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    return _escape(arg)


def launch_command(args: list[str] | None = None) -> str:
    """launch_args() as a desktop-file Exec value."""
    return " ".join(_exec_quote(a) for a in (args or launch_args()))


def autostart_enabled() -> bool:
    try:
        return autostart_path().is_file()
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    """Write or delete the autostart entry. Never raises; a failure is logged to stderr."""
    path = autostart_path()
    try:
        if not on:
            path.unlink(missing_ok=True)
            return
        args = launch_args()
        lines = ["[Desktop Entry]", "Type=Application", "Name=DragShift",
                 "Comment=Shift+drag any file to convert or edit it",
                 f"Exec={launch_command(args)}"]
        if args[1:2] == ["-m"]:          # no launcher: `-m dragshift` needs this checkout as cwd
            lines.append("Path=" + _escape(str(Path(__file__).resolve().parent.parent)))
        lines += ["Icon=dragshift", "Terminal=false", "X-GNOME-Autostart-enabled=true", "NoDisplay=false"]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        print(f"DragShift: could not {'enable' if on else 'disable'} autostart at {path}: {e}",
              file=sys.stderr)


# ---------------------------------------------------------------- desktop integration


def reveal(path: str | Path) -> bool:
    """Show `path` selected in the file manager. False = not handled, caller falls back."""
    return False

