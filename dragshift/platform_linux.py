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

import functools
import os
import shutil
import subprocess
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



# ---------------------------------------------------------------- LibreOffice
SNAP_LIBREOFFICE = "/snap/bin/libreoffice"
FLATPAK_LIBREOFFICE = "org.libreoffice.LibreOffice"


@functools.lru_cache(maxsize=1)
def soffice_binary() -> tuple[str, ...] | None:
    """Command prefix that runs LibreOffice, or None when it isn't installed. Probed once
    per process: `soffice` / `libreoffice` on PATH (deb, or a snap when /snap/bin is on
    PATH), the snap launcher, then the flatpak."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return (found,)
    if os.access(SNAP_LIBREOFFICE, os.X_OK):
        return (SNAP_LIBREOFFICE,)
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            ok = subprocess.run([flatpak, "info", FLATPAK_LIBREOFFICE], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            return (flatpak, "run", FLATPAK_LIBREOFFICE)
    return None


def soffice_workroot(cmd: tuple[str, ...]) -> Path:
    """Folder for LibreOffice's throwaway profile, input copy and output. A snap can't
    touch /tmp or hidden folders in $HOME (~/.cache included: the `home` interface skips
    dot-folders), so a snap gets its own ~/snap/<name>/common; everything else uses
    $XDG_CACHE_HOME/DragShift/soffice."""
    if cmd[0].startswith("/snap/bin/"):
        return Path.home() / "snap" / Path(cmd[0]).name.split(".")[0] / "common" / "DragShift"
    cache = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(cache) if os.path.isabs(cache) else Path.home() / ".cache"
    return base / "DragShift" / "soffice"


def kill_process_group(pgid: int) -> None:
    """SIGKILL every process in group `pgid`. A snap runs under its own AppArmor profile,
    which takes signals only from unconfined peers, so a confined caller's kill can be
    refused; whatever survives in a snap's systemd user scope (`snap run` gives each launch
    its own) is then killed through systemd."""
    import signal
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    scopes = set()
    for proc in Path("/proc").iterdir():
        try:
            if not proc.name.isdigit() or \
                    int((proc / "stat").read_text().rsplit(")", 1)[1].split()[2]) != pgid:
                continue
            cgroups = (proc / "cgroup").read_text().splitlines()
        except (OSError, ValueError, IndexError):
            continue
        scopes.update(c.rsplit("/", 1)[1] for c in cgroups if "/snap." in c and c.endswith(".scope"))
    for scope in scopes:
        try:
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", scope],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
