"""Files (Nautilus) and desktop icons (DING) in the isolated GNOME session (Linux only).

Opens folders in Nautilus, puts files on the DING desktop of the session's HOME, and
returns the stage position of a named file's icon, so tests can start real drags from
the places users drag files from:

    files = Files(gnome_session, shell_helper)
    folder = files.make_folder("pics", {"Photo.jpg": jpeg_bytes})
    files.open_folder(folder)                       # Nautilus at NAUTILUS_RECT
    x, y = files.icon_center("Photo.jpg", "nautilus")
    files.put_on_desktop({"Photo.jpg": jpeg_bytes})
    x, y = files.icon_center("Photo.jpg", "desktop")
    inject.drag_to(shell_helper, *probe.center(), start=(x, y))
    files.close()                                   # nautilus -q

How an icon is found:
- AT-SPI (preferred). The launcher's session has its own accessibility bus (org.a11y.Bus
  is activated on the private session bus). A bridge process (the system python3 with
  python3-gi, `gi_python()`, as in shell.py) walks the application's accessible tree for
  a label whose text is exactly the file name (DING inserts zero-width spaces, which are
  ignored), takes the icon image of that item and returns its extents in WINDOW
  coordinates. On Wayland a client does not know where its window is, so SCREEN
  coordinates are useless; the window's frame from the helper's ListWindows is added
  instead. For Nautilus (GTK 4) the coordinates are relative to the frame that
  ListWindows reports; DING's (GTK 3) window covers the monitor at the stage origin.
  Measured: the icon image's extents match the screenshot to the pixel, while some of
  GTK 4's intermediate containers report a 6 px offset, so the image is what counts.
- Fallback, when the accessibility bus is absent or the app exports no tree (logged as
  a warning; `Files.method` says which path answered):
  - Nautilus: the window is opened in list view, sorted by name like Nautilus does
    (folders first when the GTK setting says so, then GLib's filename collation in the
    session's locale), and the row's icon centre comes from the constants below,
    measured on Nautilus 46.4 for a window of NAUTILUS_RECT's size.
  - DING: DING stores where it put each icon in the file's GVfs metadata
    (`metadata::nautilus-icon-position`, the item's top-left on the stage); the icon
    centre is a fixed offset from it, measured on desktop-icons-ng 46+really47 with the
    default "standard" icon size.

Exact names only: "Photo.jpg" never matches "Photo (1).jpg". The returned point is
checked to lie inside the window it belongs to and not under another window (for the
desktop: not under any window), otherwise FilesError says what covers it.

The bridge part of this file (`python3 -I files.py --bridge <json>`) runs outside the
venv, so the module level only uses the standard library.
"""
from __future__ import annotations

import hashlib
import json
import locale
import mimetypes
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("tests.linux.files")

NAUTILUS_WM_CLASS = "org.gnome.Nautilus"
# Where open_folder() puts Nautilus: left half, below the boot-time "Screen Lock
# disabled" banner (top centre, y < 132), clear of the DING icon column on the
# right edge of a 1600x900 session and of the dock on the left.
NAUTILUS_RECT = (90, 150, 760, 560)
MAP_SETTLE = 0.6           # s after a window appears before it is moved (map animation)
THUMBNAIL_TIMEOUT = 30.0   # s for Nautilus to thumbnail the images of an opened folder
# An app registers on the accessibility bus a moment after it starts (DING restarts when
# its desktop folder appears); only after this long without a tree is it taken as an app
# that exports none, and the fallback is used.
NO_TREE_GRACE = 5.0

# Fallback geometry for Nautilus 46.4 list view ("medium" zoom, name column first), as
# offsets from the window frame's top-left, for a frame of NAUTILUS_RECT's size (the
# sidebar is 180 px wide up to about 900 px). Measured from screenshots against the
# AT-SPI extents in the isolated session (GNOME Shell 46.0, Yaru theme, 100 % scale).
LIST_ICON_X = 226          # centre of the row's 32x24 icon
LIST_FIRST_ICON_Y = 114    # centre of the first row's icon
LIST_ROW_PITCH = 52        # rows are 44 px plus 8 px spacing
LIST_BOTTOM_MARGIN = 20    # a row whose icon centre is closer to the bottom is cut off

# Fallback offset from DING's stored item position (metadata::nautilus-icon-position, the
# item's top-left on the stage) to its icon's centre, for the "standard" icon size on a
# 1600x900 monitor with the Ubuntu dock (item cell 135 px wide, icon 64 px tall 4 px
# below the cell top). Measured on desktop-icons-ng 46+really47.
DING_ICON_DX = 67
DING_ICON_DY = 36
DING_POSITION_ATTR = "metadata::nautilus-icon-position"

WHERE = ("nautilus", "desktop")


class FilesError(RuntimeError):
    """A window or icon could not be found, or the icon is covered."""


class Files:
    """Nautilus windows and DING desktop icons in a running `Session`.

    session: the launcher Session (tests.linux.session), or anything with its env(),
        run() and popen().
    shell: a ready ShellHelper (tests.linux.shell).
    atspi: None to use AT-SPI when the session has an accessibility bus (else the
        fallback), False to always use the fallback, True to require AT-SPI.
    bridge_env: extra environment for the AT-SPI bridge (tests set AT_SPI_BUS_ADDRESS to
        a dead socket to simulate a session without the accessibility bus).
    """

    def __init__(self, session, shell, atspi: bool | None = None,
                 bridge_env: dict | None = None):
        self.session, self.shell = session, shell
        self.atspi = atspi
        self.bridge_env = dict(bridge_env or {})
        self.method: str | None = None      # "atspi" or "fallback" for the last lookup
        self.windows: dict[Path, dict] = {}  # folder -> ListWindows record
        self.views: dict[Path, str] = {}     # folder -> "icon" | "list"
        self._last_folder: Path | None = None
        self._atspi_state: tuple[bool, str] | None = None
        self._saved_viewer: str | None = None
        self._procs: list[subprocess.Popen] = []

    def __enter__(self) -> "Files":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ setup
    def home(self) -> Path:
        return Path(self.session.env()["HOME"])

    def desktop_dir(self) -> Path:
        """The DING desktop of the session HOME (created if missing; DING notices)."""
        d = self.home() / "Desktop"
        d.mkdir(exist_ok=True)
        return d

    def make_folder(self, name: str, files: dict[str, bytes | str] | None = None) -> Path:
        """A folder in the session HOME holding `files` ({name: content})."""
        folder = self.home() / name
        folder.mkdir(parents=True, exist_ok=True)
        _write(folder, files or {})
        return folder

    def put_on_desktop(self, files: dict[str, bytes | str]) -> list[Path]:
        """Create files on the DING desktop; returns their paths."""
        return _write(self.desktop_dir(), files)

    # ------------------------------------------------------------------ AT-SPI
    def atspi_available(self) -> bool:
        """Whether lookups go through AT-SPI (cached; see the class docstring)."""
        if self.atspi is False:
            return False
        if self._atspi_state is None:
            reply = self._bridge({"cmd": "ping"})
            ok = "error" not in reply
            self._atspi_state = (ok, reply.get("error", ""))
            if not ok:
                if self.atspi:
                    raise FilesError(f"AT-SPI required but unavailable: {reply['error']}")
                log.warning("AT-SPI unavailable (%s); using the fallback geometry",
                            reply["error"])
        return self._atspi_state[0]

    def _bridge(self, request: dict, timeout: float = 30) -> dict:
        from tests.linux.session import gi_python
        try:
            r = self.session.run([gi_python(), "-I", str(Path(__file__).resolve()),
                                  "--bridge", json.dumps(request)],
                                 env=self.bridge_env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"bridge timed out after {timeout:.0f} s"}
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            # libatspi aborts the process when it cannot reach the bus.
            return {"error": f"bridge exited {r.returncode}: {r.stderr.strip()[-500:]}"}

    # ------------------------------------------------------------------ Nautilus
    def open_folder(self, folder, rect=NAUTILUS_RECT, view: str | None = None,
                    timeout: float = 20.0) -> dict:
        """Open `folder` in a new Nautilus window, move it to `rect` (x, y, w, h) and
        focus it. view: "icon" (Nautilus' default grid) or "list"; None picks "icon"
        when AT-SPI is available and "list" (needed by the fallback) otherwise.
        In icon view it also waits until the folder's images are thumbnailed (#106).
        Returns the ListWindows record of the window."""
        folder = Path(folder).resolve()
        if view is None:
            view = "icon" if self.atspi_available() else "list"
        if view not in ("icon", "list"):
            raise ValueError("view must be 'icon' or 'list'")
        self._set_viewer(f"{view}-view")
        before = {w["id"] for w in self.shell.list_windows()}
        self._procs.append(self.session.popen(
            ["nautilus", "--new-window", str(folder)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + timeout
        while True:
            new = [w for w in self.shell.list_windows()
                   if w["wm_class"] == NAUTILUS_WM_CLASS and w["id"] not in before
                   and w["title"] == folder.name]
            if new:
                break
            if time.monotonic() > deadline:
                raise FilesError(f"no Nautilus window for {folder} after {timeout:.0f} s; "
                                 f"windows: {_titles(self.shell.list_windows())}")
            time.sleep(0.1)
        win = new[-1]
        time.sleep(MAP_SETTLE)  # let the map animation finish before moving it
        frame = self.shell.move_window(win["id"], *rect)
        if frame != tuple(rect):
            log.warning("Nautilus window placed at %s instead of %s", frame, rect)
        self.shell.activate_window(win["id"])
        win = self._window(win["id"])
        self.windows[folder] = win
        self.views[folder] = view
        self._last_folder = folder
        if view == "icon":
            self.wait_for_thumbnails(folder)
        return win

    def wait_for_thumbnails(self, folder, timeout: float = THUMBNAIL_TIMEOUT) -> bool:
        """Wait until every image in `folder` has a thumbnail (or a failure record) in the
        session's thumbnail cache. Nautilus shows a placeholder until its thumbnailer has
        run, which takes seconds under load, and the placeholder looks settled: a
        screenshot taken then differs from one taken later (#106). Logs a warning and
        returns False on timeout."""
        cache = Path(self.session.env()["XDG_CACHE_HOME"]) / "thumbnails"
        images = [p for p in Path(folder).iterdir()
                  if (mimetypes.guess_type(p.name)[0] or "").startswith("image/")]
        names = {p: hashlib.md5(_glib_uri(p).encode()).hexdigest() + ".png" for p in images}
        deadline = time.monotonic() + timeout
        while True:
            missing = [p for p, n in names.items()
                       if not any(cache.glob(f"*/{n}")) and not any(cache.glob(f"fail/*/{n}"))]
            if not missing:
                return True
            if time.monotonic() > deadline:
                log.warning("no thumbnails after %.0f s for %s", timeout,
                            [p.name for p in missing])
                return False
            time.sleep(0.1)

    def _set_viewer(self, viewer: str) -> None:
        key = ("org.gnome.nautilus.preferences", "default-folder-viewer")
        if self._saved_viewer is None:
            self._saved_viewer = self._gsettings_get(*key).strip("'")
        self.session.run(["gsettings", "set", *key, viewer], check=True)

    def _gsettings_get(self, schema: str, key: str) -> str:
        return self.session.run(["gsettings", "get", schema, key], check=True).stdout.strip()

    def _window(self, window_id: int) -> dict:
        for w in self.shell.list_windows():
            if w["id"] == window_id:
                return w
        raise FilesError(f"window {window_id} is gone")

    def close(self) -> None:
        """Quit Nautilus (all its windows in this session) and restore the default
        view setting."""
        if self.windows or self._procs:
            self.session.run(["nautilus", "-q"], timeout=30)
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self._procs.clear()
        self.windows.clear()
        if self._saved_viewer:
            self.session.run(["gsettings", "set", "org.gnome.nautilus.preferences",
                              "default-folder-viewer", self._saved_viewer])
            self._saved_viewer = None

    # ------------------------------------------------------------------ lookup
    def icon_center(self, name: str, where: str = "nautilus", folder=None,
                    timeout: float = 15.0) -> tuple[int, int]:
        """Stage (x, y) of the centre of the icon of the file called exactly `name`.

        where: "nautilus" (the window of `folder`, default the last opened one) or
        "desktop" (DING). Waits up to `timeout` for the icon to appear and to stay put
        (Nautilus loads folders and thumbnails asynchronously)."""
        if where not in WHERE:
            raise ValueError(f"where must be one of {WHERE}")
        if where == "nautilus":
            folder = Path(folder).resolve() if folder else self._last_folder
            if folder not in self.windows:
                raise FilesError(f"open_folder({folder}) first")
            if self.windows[folder]["id"] not in {w["id"] for w in self.shell.list_windows()}:
                raise FilesError(f"the Nautilus window for {folder} is gone")
        start = time.monotonic()
        deadline, grace = start + timeout, start + min(NO_TREE_GRACE, timeout / 2)
        last = None
        while True:
            point, why = self._lookup(name, where, folder, time.monotonic() > grace)
            if point is not None and point == last:
                break
            last = point
            if time.monotonic() > deadline:
                if point is not None:
                    break  # found, but still moving: use the latest position
                raise FilesError(f"no icon for {name!r} in {where} after {timeout:.0f} s: "
                                 f"{why}")
            time.sleep(0.25)
        self._check_visible(point, where, folder)
        return point

    def _lookup(self, name, where, folder, give_up_on_tree: bool):
        """(point or None, reason)."""
        if self.atspi_available():
            frame = (self._window(self.windows[folder]["id"])["frame"]
                     if where == "nautilus" else None)
            reply = self._bridge({"cmd": "find", "app": where, "name": name,
                                  "frame": folder.name if where == "nautilus" else None})
            if "error" not in reply:
                matches = reply["matches"]
                if len(matches) > 1:
                    raise FilesError(f"{len(matches)} icons named {name!r} in {where}: "
                                     f"{matches}")
                if matches:
                    self.method = "atspi"
                    x, y, w, h = matches[0]["icon"]
                    ox, oy = (frame[0], frame[1]) if frame else (0, 0)
                    return (ox + x + w // 2, oy + y + h // 2), ""
                return None, f"AT-SPI found no item (frames: {reply['frames']})"
            if not reply.get("no_tree") or not give_up_on_tree:
                return None, reply["error"]
            log.warning("AT-SPI: %s; using the fallback geometry", reply["error"])
        self.method = "fallback"
        if where == "desktop":
            return self._ding_fallback(name)
        return self._list_fallback(name, folder)

    def _ding_fallback(self, name):
        path = self.desktop_dir() / name
        if not path.exists():
            raise FilesError(f"{path} does not exist")
        reply = self._bridge({"cmd": "metadata", "path": str(path),
                              "attr": DING_POSITION_ATTR})
        value = reply.get("value") or ""
        try:
            x, y = (int(float(v)) for v in value.split(","))
        except ValueError:
            return None, f"DING has not placed {name!r} yet ({DING_POSITION_ATTR}={value!r})"
        return (x + DING_ICON_DX, y + DING_ICON_DY), ""

    def _list_fallback(self, name, folder):
        if self.views.get(folder) != "list":
            raise FilesError(f"the fallback needs list view; open_folder({folder}) was "
                             f"opened in {self.views.get(folder)} view")
        _, _, w, h = frame = self._window(self.windows[folder]["id"])["frame"]
        if (w, h) != tuple(NAUTILUS_RECT[2:]):
            raise FilesError(f"fallback geometry is measured for a {NAUTILUS_RECT[2]}x"
                             f"{NAUTILUS_RECT[3]} window, not {w}x{h}")
        order = self.list_order(folder)
        if name not in order:
            return None, f"{name!r} is not in {folder} (shown: {order})"
        y = LIST_FIRST_ICON_Y + order.index(name) * LIST_ROW_PITCH
        if y > h - LIST_BOTTOM_MARGIN:
            raise FilesError(f"{name!r} is row {order.index(name)}, below the visible rows")
        return (frame[0] + LIST_ICON_X, frame[1] + y), ""

    def list_order(self, folder) -> list[str]:
        """The names Nautilus' list view shows for `folder`, top to bottom (sorted by
        name, ascending, hidden files left out)."""
        folder = Path(folder)
        sort = self._gsettings_get("org.gnome.nautilus.preferences", "default-sort-order")
        rev = self._gsettings_get("org.gnome.nautilus.preferences",
                                  "default-sort-in-reverse-order")
        if sort != "'name'" or rev != "false":
            raise FilesError(f"fallback expects sorting by name, ascending ({sort}, {rev})")
        hidden = set()
        try:
            hidden = set((folder / ".hidden").read_text().split("\n"))
        except OSError:
            pass
        entries = [p for p in folder.iterdir() if not p.name.startswith(".")
                   and not p.name.endswith("~") and p.name not in hidden]
        dirs_first = self._gsettings_get("org.gtk.gtk4.Settings.FileChooser",
                                         "sort-directories-first") == "true"
        keys = self._bridge({"cmd": "collate", "names": [p.name for p in entries]})["keys"]
        ranked = sorted(zip(entries, keys),
                        key=lambda e: (dirs_first and not e[0].is_dir(), e[1]))
        return [p.name for p, _ in ranked]

    def _check_visible(self, point, where, folder) -> None:
        x, y = point
        windows = [w for w in self.shell.list_windows() if not w["minimized"]
                   and w["window_type"] not in ("desktop", "dock")]
        inside = [w for w in windows if _contains(w["frame"], x, y)]
        if where == "desktop":
            if inside:
                raise FilesError(f"desktop icon at {point} is covered by {_titles(inside)}")
            return
        mine = self.windows[folder]["id"]
        frame = self._window(mine)["frame"]
        if not _contains(frame, x, y):
            raise FilesError(f"icon at {point} is outside its window {frame} (scrolled?)")
        stacking = {w["id"]: w["stacking"] for w in windows}
        above = [w for w in inside if w["stacking"] > stacking.get(mine, -1)]
        if above:
            raise FilesError(f"icon at {point} is covered by {_titles(above)}")


def _glib_uri(path: Path) -> str:
    """The file:// URI as g_filename_to_uri() spells it (the thumbnail cache key): it
    leaves !$&'()*+,:=@ unescaped, which Path.as_uri() escapes."""
    return "file://" + quote(str(Path(path).resolve()), safe="/!$&'()*+,:=@")


def _write(folder: Path, files: dict) -> list[Path]:
    paths = []
    for name, content in files.items():
        p = folder / name
        if isinstance(content, str):
            p.write_text(content)
        else:
            p.write_bytes(content)
        paths.append(p)
    return paths


def _contains(frame, x, y) -> bool:
    fx, fy, fw, fh = frame
    return fx <= x < fx + fw and fy <= y < fy + fh


def _titles(windows) -> list:
    return [(w["wm_class"], w["title"]) for w in windows]


# ---------------------------------------------------------------------- bridge
# Runs as `python3 -I files.py --bridge <json>` with the system python3 (python3-gi)
# inside the session environment; prints one JSON line.

_ICON_ROLES = ("image", "icon")
_ITEM_ROLES = ("table cell", "list item", "table row", "tree item")


def _a11y_bus_error():
    """Why the session has no reachable accessibility bus, or None."""
    from gi.repository import Gio, GLib
    addr = os.environ.get("AT_SPI_BUS_ADDRESS")
    try:
        if not addr:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            addr = bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
                                 "GetAddress", None, GLib.VariantType("(s)"),
                                 Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        Gio.DBusConnection.new_for_address_sync(
            addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None).close_sync(None)
    except GLib.Error as e:
        return f"no accessibility bus ({e.message})"
    return None


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _is_app(app, where: str) -> bool:
    cmd = _cmdline(app.get_process_id())
    if where == "desktop":  # DING runs as `gjs <ext dir>/app/ding.js`
        return "ding@rastersoft.com/app/ding.js" in cmd
    return app.get_name() == NAUTILUS_WM_CLASS or cmd.split(" ")[0].endswith("nautilus")


def _children(node):
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            yield child


def _extents(node, Atspi):
    e = node.get_extents(Atspi.CoordType.WINDOW)
    return [e.x, e.y, e.width, e.height]


def _find_in(frame, name: str, where: str, Atspi) -> list[dict]:
    """Items under `frame` whose label is exactly `name`."""
    matches = []

    def first_icon(node):
        for child in _children(node):
            if child.get_role_name() in _ICON_ROLES:
                return child
            found = first_icon(child)
            if found is not None:
                return found
        return None

    def walk(node, ancestors):
        role = node.get_role_name()
        if role == "label" and (node.get_name() or "").replace("​", "") == name:
            # The item is the nearest ancestor that also holds the icon.
            for anc in reversed(ancestors):
                icon = first_icon(anc)
                if icon is not None:
                    break
            else:
                return
            if where == "nautilus" and not any(
                    a.get_role_name() in _ITEM_ROLES and a.get_name() == name
                    for a in ancestors):
                return  # a label elsewhere (path bar, sidebar), not a file item
            box = _extents(icon, Atspi)
            if box[2] <= 0 or box[3] <= 0:
                box = _extents(anc, Atspi)
            matches.append({"icon": box, "item": _extents(anc, Atspi),
                            "label": _extents(node, Atspi)})
            return
        for child in _children(node):
            walk(child, ancestors + [node])

    walk(frame, [])
    return matches


def _bridge_find(req) -> dict:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    desktop = Atspi.get_desktop(0)
    apps = [a for a in _children(desktop) if _is_app(a, req["app"])]
    if not apps:
        return {"error": f"{req['app']} has no accessible tree", "no_tree": True}
    frames, matches = [], []
    for app in apps:
        for frame in _children(app):
            title = frame.get_name()
            frames.append(title)
            if req["app"] == "nautilus" and req.get("frame") and title != req["frame"]:
                continue
            matches += _find_in(frame, req["name"], req["app"], Atspi)
    return {"matches": matches, "frames": frames}


def _bridge(req) -> dict:
    cmd = req["cmd"]
    if cmd == "collate":
        locale.setlocale(locale.LC_ALL, "")  # Nautilus sorts in the session's locale
        from gi.repository import GLib
        return {"keys": [GLib.utf8_collate_key_for_filename(n, -1) for n in req["names"]]}
    if cmd == "metadata":
        from gi.repository import Gio, GLib
        try:
            info = Gio.File.new_for_path(req["path"]).query_info(
                req["attr"], Gio.FileQueryInfoFlags.NONE, None)
        except GLib.Error as e:
            return {"error": e.message}
        return {"value": info.get_attribute_as_string(req["attr"])}
    err = _a11y_bus_error()
    if err:
        return {"error": err}
    if cmd == "ping":
        return {}
    if cmd == "find":
        return _bridge_find(req)
    return {"error": f"unknown command {cmd!r}"}


if __name__ == "__main__" and sys.argv[1:2] == ["--bridge"]:
    print(json.dumps(_bridge(json.loads(sys.argv[2]))), flush=True)
