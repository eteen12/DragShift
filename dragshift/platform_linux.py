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

import functools
import os
import shutil
import subprocess
import sys
import threading
import time
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
# After a failed call the client stays off the bus for a while (0.5 s, doubling up to 5 s;
# reset by the next success), so a stopped or hung extension costs one call per back-off
# step instead of one per 15 ms trigger tick.
BACKOFF_FIRST, BACKOFF_MAX = 0.5, 5.0

_answered = False    # the last GetPointer call succeeded
_retry_at = 0.0      # time.monotonic() before which a failed client doesn't call again
_backoff = 0.0


def _get_pointer() -> tuple | None:
    """One GetPointer call with the back-off bookkeeping: the reply body, or None (at once,
    without touching the bus, while backing off)."""
    global _answered, _retry_at, _backoff
    if not _answered and time.monotonic() < _retry_at:
        return None
    body = _call("GetPointer")
    if body is not None and len(body) == 3:
        _answered, _backoff = True, 0.0
        return body
    _backoff = min(BACKOFF_MAX, _backoff * 2 or BACKOFF_FIRST)
    _answered, _retry_at = False, time.monotonic() + _backoff
    return None


def pointer_state() -> tuple[int, int, bool, int] | None:
    """Global pointer `(x, y, left_down, modifier_mask)` from the shell extension, or None
    when unknown (extension missing, stopped or not answering within DBUS_TIMEOUT; then
    None until the back-off allows the next try). Never raises.

    x, y are global logical pixels across all monitors (at scale 1 equal to X/Qt pixels).
    modifier_mask is Clutter.ModifierType as is: compare it with KEY_MASKS / BUTTON1_MASK
    and ignore other bits (Num Lock, Super, ...). Never derived from QCursor.pos(): under
    XWayland it is stale while the pointer is over Wayland windows."""
    body = _get_pointer()
    if body is None:
        return None
    x, y, mask = body
    return x, y, bool(mask & BUTTON1_MASK), mask


def extension_available() -> bool:
    """True when DragShift's GNOME Shell extension answered the last call. While it hasn't,
    this tries again (one GetPointer call) whenever the back-off allows, so calling it from
    a slow timer notices an extension that is installed or enabled later."""
    if not _answered:
        _get_pointer()
    return _answered


# ---------------------------------------------------------------- screen capture
def capture(x: int, y: int, w: int, h: int, exclude_self: bool = False):
    """Screen rect (logical px) as a PIL RGB image, or None. The drag picture (Mutter's
    drag actor) is ALWAYS left out. exclude_self additionally leaves DragShift's own
    windows out, for live refresh behind toasts and tool windows."""
    return None


def wallpaper_crop(x: int, y: int, w: int, h: int):
    """The desktop wallpaper under a screen rect as a PIL RGB image of size w×h, or None.

    Unblurred: callers blur it. Drawn as GNOME draws it on the monitor that contains
    (x, y); any part of the rect off that monitor is the background colour. No picture
    (`none`, a missing or undecodable file) gives a solid `primary-color`. None only when
    the GNOME settings can't be read (not GNOME) or there is no screen to map onto."""
    import os
    from PIL import Image
    s = _read_settings()
    mon = _monitor_at(x, y)
    if s is None or mon is None or w <= 0 or h <= 0:
        return None
    mx, my, mw, mh = mon
    bg = _parse_color(s.get("primary-color", ""))
    dark = s.get("color-scheme") == "prefer-dark" and s.get("picture-uri-dark")
    path = _uri_path(s.get("picture-uri-dark") if dark else s.get("picture-uri", ""))
    try:
        st = os.stat(path) if path and s.get("picture-options") != "none" else None
    except OSError:
        st = None    # deleted file or dangling symlink
    key = (path, st and st.st_mtime_ns, st and st.st_size, bg, mon)
    if st is not None and key not in _WALL_CACHE:
        if len(_WALL_CACHE) >= 4:
            _WALL_CACHE.clear()
        _WALL_CACHE[key] = _decode_zoom(path, mw, mh, bg)
    full = _WALL_CACHE.get(key)
    out = Image.new("RGB", (w, h), bg)
    if full is not None:
        out.paste(full, (mx - x, my - y))
    return out


# Wallpaper under a monitor, decoded once at monitor size: (path, mtime, size, colour,
# monitor geometry) -> image, or None when the file didn't decode.
_WALL_CACHE: dict = {}
WALLPAPER_KEYS = ("picture-uri", "picture-uri-dark", "picture-options", "primary-color")


def _read_settings() -> dict | None:
    """The wallpaper keys of org.gnome.desktop.background plus the interface colour-scheme,
    read with the `gsettings` tool (read-only; `gi` isn't importable in the app's venv).
    Two processes in parallel, ~4 ms. None when gsettings or the GNOME schemas are missing."""
    import ast
    import subprocess
    cmds = (["gsettings", "list-recursively", "org.gnome.desktop.background"],
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
    procs = []
    try:
        for c in cmds:
            procs.append(subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          text=True))
        (bg, _), (scheme, _) = (p.communicate(timeout=2) for p in procs)
    except (OSError, subprocess.SubprocessError):
        for p in procs:
            p.kill()
            p.wait()
        return None
    if procs[0].returncode != 0:
        return None
    lines = [ln.split(" ", 2)[1:] for ln in bg.splitlines()]
    if procs[1].returncode == 0:          # older GNOME has no dark style: use picture-uri
        lines.append(["color-scheme", scheme.strip()])
    out = {}
    for key, text in (ln for ln in lines if len(ln) == 2):
        if key in WALLPAPER_KEYS or key == "color-scheme":
            try:
                out[key] = str(ast.literal_eval(text))   # GVariant text: 'a string'
            except (ValueError, SyntaxError):
                out[key] = text.strip("'\"")
    return out if "picture-uri" in out else None


def _monitor_at(x: int, y: int) -> tuple[int, int, int, int] | None:
    """Logical geometry (x, y, w, h) of the monitor that contains (x, y), else the
    primary monitor; None without a running Qt GUI application."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        return None
    scr = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
    if scr is None:
        return None
    g = scr.geometry()
    return g.x(), g.y(), g.width(), g.height()


def _uri_path(uri: str) -> str:
    """Local path of a file:// URI ('' for anything else, e.g. resource:// or empty)."""
    from urllib.parse import unquote, urlparse
    u = urlparse(uri or "")
    return unquote(u.path) if u.scheme == "file" else ""


def _parse_color(text: str) -> tuple[int, int, int]:
    """GNOME colour string -> RGB: #rgb, #rrggbb, or 16 bits per channel #rrrrggggbbbb."""
    h = (text or "").strip().lstrip("#")
    if len(h) in (3, 6, 9, 12) and all(c in "0123456789abcdefABCDEF" for c in h):
        n = len(h) // 3
        return tuple(int(h[i * n:(i + 1) * n], 16) * 255 // (16 ** n - 1) for i in range(3))
    return (0, 0, 0)


def _decode_zoom(path: str, mw: int, mh: int, bg: tuple[int, int, int]):
    """The picture as GNOME's `zoom` draws it on an mw×mh monitor: scaled to cover it,
    centred, the overflow cropped (never squashed). Every other picture-option is drawn
    the same way (scaled/stretched/centered/wallpaper/spanned are approximations).
    None when the file isn't a decodable image (e.g. an XML slideshow)."""
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as src:
            turned = src.getexif().get(0x0112, 1) in (5, 6, 7, 8)   # EXIF: rotated 90°
            sw, sh = src.size[::-1] if turned else src.size
            k = max(mw / sw, mh / sh)
            want = (round(sw * k), round(sh * k))
            src.draft("RGB", want[::-1] if turned else want)  # JPEG: decode at >= that size
            im = ImageOps.exif_transpose(src)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, bg)
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
        im = im.convert("RGB")
    except Exception:
        return None
    sw, sh = im.size
    k = max(mw / sw, mh / sh)
    cw, ch = mw / k, mh / k                                    # monitor in source pixels
    box = ((sw - cw) / 2, (sh - ch) / 2, (sw + cw) / 2, (sh + ch) / 2)
    return im.resize((mw, mh), Image.BICUBIC, box=box, reducing_gap=2.0)


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
    """Show `path` selected in the file manager (GNOME Files) through
    org.freedesktop.FileManager1.ShowItems. False = not handled, caller falls back.

    The call is asynchronous, so the UI never waits while D-Bus starts Files. If it fails
    later (no FileManager1 service, timeout, …) the folder is opened with `xdg-open`."""
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher
    except ImportError:
        return False
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    msg = QDBusMessage.createMethodCall("org.freedesktop.FileManager1",
                                        "/org/freedesktop/FileManager1",
                                        "org.freedesktop.FileManager1", "ShowItems")
    msg.setArguments([[Path(path).absolute().as_uri()], ""])
    # parented to the app, so the watcher outlives this call until the reply arrives
    watcher = QDBusPendingCallWatcher(bus.asyncCall(msg, 10_000), QCoreApplication.instance())

    def done(w):
        if w.isError():
            import subprocess
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
        w.deleteLater()

    watcher.finished.connect(done)
    return True



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
