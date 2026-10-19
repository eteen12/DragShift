"""DragShift: Shift+drag any file to convert or edit it from a radial liquid-glass menu."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QDir, QObject, QPoint, QPointF, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QPainter,
                           QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon, QWidget

from . import __version__, convert, glass, imaging, report, tools
from .radial import Item, RadialMenu
from .settings import Settings, SettingsWindow, WelcomeWindow, TRIGGERS
from .toasts import ToastStack
from .trigger import DragShiftWatcher
from .util import UserError, category, file_size, human_size, keep, release

APP_NAME = "DragShift"
TIP_URL = "https://cash.app/$AtikaKadesha"
VERSION = __version__
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
from .handoff import ipc_name
IPC_NAME = ipc_name()

GLYPHS = {"Compress": "⇲", "Metadata": "ⓘ", "Redact": "▮", "Crop": "⌗", "Edit": "☼",
          "Annotate": "✎", "Add BG": "▣", "Resize": "⤢", "Split": "✂", "Rotate": "⟳",
          "Merge": "⧉", "Protect": "⚿", "Extract": "⧈"}

HINTS = {
    "Compress": "Shrink file size", "Metadata": "View · remove GPS", "Redact": "Hide private info",
    "Crop": "Trim to a ratio", "Edit": "Light · colour · rotate", "Annotate": "Arrows · text · steps",
    "Add BG": "Gradient backdrop", "Resize": "Change dimensions", "Split": "One PDF per page",
    "Rotate": "Turn pages 90°", "Merge": "Join into one PDF", "Protect": "Add a password",
    "Extract": "Save embedded images",
}
FORMAT_HINTS = {"JPG": "Small, universal", "PNG": "Lossless · transparency", "WEBP": "Smallest for web",
                "HEIC": "iPhone format", "TIFF": "Print · archival", "AVIF": "Next-gen, tiny",
                "BMP": "Uncompressed", "PDF": "Document", "ICO": "Windows icon", "GIF": "Legacy web",
                "DOCX": "Editable Word", "TXT": "Plain text"}

IMAGE_TOOLS = ["Compress", "Resize", "Crop", "Edit", "Annotate", "Redact", "Add BG", "Metadata"]
PDF_TOOLS = ["Compress", "Split", "Rotate", "Protect", "Extract", "Metadata"]


# ---------------------------------------------------------------- background jobs
class _Sig(QObject):
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    def __init__(self, action: str, srcs: list, fn, extra: str = ""):
        super().__init__()
        self.action, self.srcs, self.fn, self.extra = action, srcs, fn, extra
        self.sig = _Sig()

    def run(self):
        try:
            outs = self.fn() or []
            self.sig.done.emit(report.summarize(self.action, self.srcs, outs, self.extra))
        except UserError as e:
            self.sig.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.sig.failed.emit(friendly_error(e))


def friendly_error(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    low = msg.lower()
    if isinstance(e, PermissionError) or "permission denied" in low:
        return "No permission to write next to this file. Try a folder you own, or set Settings → Save results → subfolder."
    if "cannot identify image" in low:
        return "This file doesn't look like an image DragShift can read."
    if isinstance(e, FileNotFoundError):
        return "The file was moved or deleted."
    return msg


# ---------------------------------------------------------------- icon
def no_tray() -> bool:
    """Linux: True while no StatusNotifier host shows tray items (GNOME without Ubuntu's
    AppIndicator extension, or with it disabled). Qt asks the session bus on every call."""
    return IS_LINUX and not QSystemTrayIcon.isSystemTrayAvailable()


def open_url(url: str) -> None:
    if IS_LINUX:
        # Qt hands URLs to the desktop portal, which asks "Open with…" instead of starting
        # the default browser; xdg-open (gio open on GNOME) starts it directly.
        try:
            subprocess.Popen(["xdg-open", url])
            return
        except OSError:
            pass
    QDesktopServices.openUrl(QUrl(url))


def make_tray_icon() -> QIcon:
    """macOS menu bar wants a monochrome 'template' image; Windows and Linux get the colour
    icon. On Linux Qt sends the .ico's 16-64 px frames as the StatusNotifierItem's
    IconPixmap, and GNOME's AppIndicator extension draws the smallest frame of at least
    16 × the monitor scale: 16 px at 100 %, 32 px at 200 %, each 1:1."""
    if not IS_MAC:
        return make_icon()
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor(0, 0, 0), 4.5))
    p.drawEllipse(QPointF(22, 22), 14, 14)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0))
    p.drawEllipse(QPointF(22, 22), 5, 5)
    for a in range(0, 360, 45):  # petal gaps
        import math
        x, y = 22 + 14 * math.cos(math.radians(a + 22.5)), 22 + 14 * math.sin(math.radians(a + 22.5))
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.end()
    pm.setDevicePixelRatio(2.0)
    icon = QIcon(pm)
    icon.setIsMask(True)
    return icon


def make_icon() -> QIcon:
    ico = Path(__file__).with_name("assets") / "dragshift.ico"
    if ico.exists():
        return QIcon(str(ico))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(glass.iridescent_pen(QPointF(32, 32), 7, 255))
    p.drawEllipse(QPointF(32, 32), 24, 24)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- the app
class DragShiftApp(QObject):
    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.settings = Settings()
        self.settings.apply()
        self.pool = QThreadPool.globalInstance()
        self.windows: list[QWidget] = []
        self.toasts = ToastStack()
        self.menu = RadialMenu(self.build_rings, self.describe)
        self.menu.activated.connect(self.run_item)

        # the watcher only manages menus it opened (drag mode); click-mode menus (tray, right-click,
        # Send To) must not be closed just because no mouse button is held
        self.watcher = DragShiftWatcher(lambda: self.menu.isVisible() and not self.menu.click_mode,
                                        lambda: self.menu.got_drag, self,
                                        self.menu.drag_cancelled_since)
        self.watcher.set_key(self.settings.get("trigger"))
        self.watcher.show_menu.connect(lambda pos: self.menu.popup(pos))
        self.watcher.toggle_ring.connect(self.menu.next_ring)
        if IS_LINUX:  # which ring a Shift tap flipped to (read by tests/linux/test_trigger.py)
            self.watcher.toggle_ring.connect(lambda: print(
                "ring:", self.menu.ring_names[self.menu.ring_idx] if self.menu.ring_names else "-",
                flush=True))
        self.watcher.hide_menu.connect(self.menu.close_menu)
        self.watcher.start()
        if IS_LINUX:  # the menu's wallpaper fallback, decoded now rather than on its first popup
            from . import platform_linux
            platform_linux.warm_wallpaper()

        # files handed over by "Send to", the right-click menu, or a second launch
        self._pending: list[str] = []
        self._pending_timer = QTimer(self, singleShot=True, interval=280, timeout=self._flush_pending)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(IPC_NAME)
        self.server.listen(IPC_NAME)
        self.server.newConnection.connect(self._ipc)

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} — press {self.trigger_label()} while dragging a file")
        m = QMenu()
        m.setStyleSheet(glass.QSS)
        self.act_enabled = QAction("Enabled", m, checkable=True, checked=True)
        self.act_enabled.toggled.connect(self._set_enabled)
        m.addAction(self.act_enabled)
        m.addAction("Open a file…", self.open_dialog)
        m.addSeparator()
        m.addAction("Settings…", self.open_settings)
        m.addAction("How to use", self.show_welcome)
        m.addAction("Tip the developer ♥", lambda: open_url(TIP_URL))
        m.addSeparator()
        m.addAction(f"Quit {APP_NAME}", self.qapp.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()
        if no_tray():
            # Qt keeps the item and registers it if a host appears later (AppIndicator
            # enabled, another desktop's panel); until then a second launch opens Settings
            log_line("tray: no StatusNotifierWatcher; use Files → Scripts → DragShift "
                     "or `dragshift <file>`")

    def trigger_label(self) -> str:
        return dict(TRIGGERS).get(self.settings.get("trigger"), "Shift")

    # ------------------------------------------------------------ IPC
    def _ipc(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return
        buf = bytearray()
        done = {"v": False}

        def consume():
            if done["v"]:
                return
            try:
                buf.extend(bytes(sock.readAll()))
            except RuntimeError:
                pass
            try:
                data = json.loads(buf.decode("utf-8"))
            except Exception:
                return  # incomplete: wait for more
            done["v"] = True
            files = [f for f in data.get("files", []) if os.path.exists(f)]
            print(f"IPC: {len(files)} file(s) handed over", flush=True)
            if files:
                self.queue_files(files)
            elif data.get("show"):
                self.show_requested()
            sock.disconnectFromServer()
            sock.deleteLater()

        # Windows pipes: the sender may already be gone, so read on every signal + a fallback timer
        sock.readyRead.connect(consume)
        sock.disconnected.connect(consume)
        for ms in (0, 50, 200, 600):
            QTimer.singleShot(ms, consume)

    def queue_files(self, files: list[str]):
        """Explorer's right-click menu launches once per file: gather them, then show one menu."""
        for f in files:
            if f not in self._pending:
                self._pending.append(f)
        self._pending_timer.start()

    def _flush_pending(self):
        files, self._pending = self._pending, []
        if files:
            self.show_click_menu(files)

    # ------------------------------------------------------------ menu contents
    def describe(self, paths: list[str]) -> str:
        if len(paths) > 1:
            return f"{len(paths)} files"
        return human_size(file_size(paths[0]))

    def build_rings(self, paths: list[str]) -> dict[str, list[Item]]:
        cat = category(paths[0])
        if cat is None:
            return {"Unsupported": []}
        ext = Path(paths[0]).suffix
        same = [p for p in paths if category(p) == cat]
        convert_items = []
        for t in convert.targets_for(cat, ext):
            hint = FORMAT_HINTS.get(t, "")
            if cat == "image" and t == "PDF" and len(same) > 1:
                hint = f"{len(same)} images → 1 PDF"
            convert_items.append(Item(t, key=f"convert:{t}", hint=hint))
        rings: dict[str, list[Item]] = {"Convert": convert_items}
        if cat == "image":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in IMAGE_TOOLS]
        elif cat == "pdf":
            names = PDF_TOOLS[:3] + (["Merge"] if len(same) > 1 else []) + PDF_TOOLS[3:]
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in names]
        return rings

    # ------------------------------------------------------------ actions
    def run_item(self, item: Item, paths: list[str]):
        cat = category(paths[0])
        paths = [p for p in paths if category(p) == cat]
        kind, name = item.key.split(":", 1)
        if kind == "convert":
            if cat == "image" and name == "PDF" and len(paths) > 1:
                self.submit(f"convert:{name}", paths, lambda: convert.images_to_single_pdf(paths),
                            f"Combining {len(paths)} images…")
            else:
                verb = f"Converting to {name}…"
                self.submit(f"convert:{name}", paths,
                            lambda: [o for p in paths for o in convert.convert(p, cat, name)], verb)
            return
        if cat == "pdf":
            if name == "Merge":
                self.submit("tool:Merge", paths, lambda: [imaging.merge_pdfs(paths)], "Merging…")
            elif name == "Metadata":
                self.open_window(tools.MetadataWindow(paths[0], self.notify, is_pdf=True))
            elif name == "Protect":
                self.open_window(tools.ProtectWindow(paths[0], self.notify))
            else:
                fns = {"Compress": lambda p: [imaging.compress_pdf(p)], "Split": imaging.split_pdf,
                       "Rotate": lambda p: [imaging.rotate_pdf(p)], "Extract": imaging.extract_pdf_images}
                f = fns[name]
                self.submit(f"tool:{name}", paths, lambda: [o for p in paths for o in f(p)],
                            {"Compress": "Compressing…", "Split": "Splitting…", "Rotate": "Rotating…",
                             "Extract": "Extracting images…"}[name])
            return
        windows = {"Crop": tools.CropWindow, "Add BG": tools.AddBgWindow, "Annotate": tools.AnnotateWindow,
                   "Redact": tools.RedactWindow, "Edit": tools.EditWindow, "Metadata": tools.MetadataWindow}
        try:
            if name == "Compress":
                self.open_window(tools.CompressWindow(paths, self.notify))
            elif name == "Resize":
                self.open_window(tools.ResizeWindow(paths, self.notify))
            else:
                self.open_window(windows[name](paths[0], self.notify))
        except UserError as e:
            self.toasts.error(f"Couldn't open {name}", str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.toasts.error(f"Couldn't open {name}", friendly_error(e))

    def open_window(self, w: QWidget):
        if hasattr(w, "submit"):
            w.submit = self.submit
        self.windows.append(w)
        w.destroyed.connect(lambda *_: self.windows.remove(w) if w in self.windows else None)
        if hasattr(w, "present"):
            w.present()
        else:
            w.show()

    def submit(self, action: str, srcs: list, fn, working: str = "Working…", extra: str = ""):
        """Run in the background. A spinner card appears only if it takes > 150 ms."""
        state = {"card": None, "finished": False}
        names = Path(srcs[0]).name if len(srcs) == 1 else f"{len(srcs)} files"

        def show_spinner():
            if not state["finished"]:
                state["card"] = self.toasts.new()
                state["card"].working(working, names)

        QTimer.singleShot(150, show_spinner)
        job = Job(action, srcs, fn, extra)

        def done(r):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.done(r)

        def failed(msg):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.error("Couldn't finish", msg)

        job.sig.done.connect(done)
        job.sig.failed.connect(failed)
        self.pool.start(keep(job))

    def notify(self, action: str, srcs: list, outs: list, extra: str = ""):
        """Called by tool windows after they save."""
        self.toasts.new().done(report.summarize(action, srcs, outs, extra))

    # ------------------------------------------------------------ misc
    def show_requested(self):
        """DragShift was launched again while running (no files)."""
        if no_tray():  # Linux without a StatusNotifier host: no menu to reach Settings from
            self.open_settings()
        else:
            self.toasts.info("DragShift is already running",
                             f"Drag any file and press {self.trigger_label()}. It lives in the tray.")

    def running_text(self) -> str:
        """Body of the start-up "DragShift is running" toast."""
        text = f"Drag any file, then press {self.trigger_label()}."
        return text + " Open DragShift again for Settings." if no_tray() else text

    def _set_enabled(self, on: bool):
        self.watcher.enabled = on
        log_line(f"tray: trigger {'enabled' if on else 'disabled'}")

    def _tray_click(self, reason):
        # macOS: a click on the menu-bar icon shows the menu; Windows: click opens a file.
        # GNOME (AppIndicator): a click shows the menu, a double click sends Activate,
        # which Qt reports as Trigger, so it opens a file as on Windows.
        if not IS_MAC and reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.open_dialog()

    def open_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Choose file(s) for DragShift", QDir.homePath())
        if files:
            self.show_click_menu(files)

    def show_click_menu(self, files: list[str]):
        if IS_LINUX:
            # no activateWindow(): on Wayland it can only ask for attention, and the menu
            # never takes the focus anyway (#10, #36)
            self.menu.popup(linux_click_pos(), files)
            return
        g = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        pos = QCursor.pos() if g.contains(QCursor.pos()) else g.center()
        self.menu.popup(pos, files)
        self.menu.activateWindow()

    def open_settings(self):
        w = SettingsWindow(self.settings)
        w.changed.connect(self._settings_changed)
        self.open_window(w)

    def _settings_changed(self):
        self.watcher.set_key(self.settings.get("trigger"))
        self.tray.setToolTip(f"{APP_NAME} — press {self.trigger_label()} while dragging a file")

    def show_welcome(self):
        self.open_window(WelcomeWindow(self.settings, self.trigger_label()))


def linux_click_pos() -> QPoint:
    """Linux: where a click-mode menu opens. The pointer from DragShift's shell extension
    (QCursor.pos() is stale under Wayland), else the centre of the primary screen."""
    from . import platform_linux
    st = platform_linux.pointer_state()
    if st is not None:
        return QPoint(int(st[0]), int(st[1]))
    return QGuiApplication.primaryScreen().geometry().center()


class DragShiftQApplication(QApplication):
    """macOS delivers files from Finder's "Open With", the Dock and `open -a` as FileOpen
    events (not argv)."""
    file_opened = Signal(str)

    def __init__(self, argv):
        super().__init__(argv)
        self.early_files: list[str] = []

    def event(self, e):
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.FileOpen:
            f = e.file()
            if f:
                self.early_files.append(f)
                self.file_opened.emit(f)
            return True
        return super().event(e)


def mac_setup(qapp):
    from PySide6.QtGui import QFont
    for fam in ("Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI"):
        QFont.insertSubstitutions(fam, [".AppleSystemUIFont", "SF Pro Display", "Helvetica Neue"])
    try:
        from . import platform_mac
        platform_mac.become_accessory()
    except Exception:
        pass


# Linux app id: the Wayland app_id of every window, so GNOME matches them to
# <LINUX_APP_ID>.desktop (dock icon and name, tray). Same reverse-DNS name as the D-Bus service.
LINUX_APP_ID = "io.github.eteen12.DragShift"
# Installed on a default Ubuntu 24.04 desktop: Ubuntu Sans, Ubuntu, Noto Sans, DejaVu Sans.
LINUX_FONTS = ["Ubuntu Sans", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans"]


def linux_qpa(env=None, argv=()) -> str | None:
    """Linux: the Qt platform to ask for before QApplication exists, or None for Qt's default
    (docs/linux/display-backend.md). A QT_QPA_PLATFORM or -platform the user gave always
    wins; DRAGSHIFT_QPA picks one explicitly (tests); a Wayland session gets native Wayland
    with Xwayland as Qt's fallback; an X11 session and anything else keep Qt's default."""
    env = os.environ if env is None else env
    if env.get("QT_QPA_PLATFORM") or "-platform" in argv:
        return None
    if env.get("DRAGSHIFT_QPA"):
        return env["DRAGSHIFT_QPA"]
    session = env.get("XDG_SESSION_TYPE", "")
    if session != "x11" and (env.get("WAYLAND_DISPLAY") or session == "wayland"):
        return "wayland;xcb"
    return None


def linux_setup(qapp):
    """Linux, right after QApplication exists and before any window: Segoe UI → installed
    fonts, the app id, and the platform, screens and font in the log. HiDPI needs nothing
    more: under Wayland, PassThrough (main) gives each window the monitor's exact scale,
    also a fractional one (1.25, 1.5…), so text and glass are drawn at device pixels."""
    from PySide6.QtGui import QFont, QFontInfo
    for fam in ("Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI"):
        QFont.insertSubstitutions(fam, LINUX_FONTS)
    qapp.setDesktopFileName(LINUX_APP_ID)
    log_line(f"platform: {qapp.platformName()}")
    for s in qapp.screens():  # Wayland: the output's integer scale; windows may use less
        g = s.geometry()
        log_line(f"screen {s.name()}: {g.width()}x{g.height()} at ({g.x()}, {g.y()}), "
                 f"output scale {s.devicePixelRatio():g}")
    log_line(f"font: Segoe UI → {QFontInfo(QFont('Segoe UI Variable Display')).family()}")


def check_extension_version():
    """Linux: say when DragShift's GNOME Shell extension is missing or was built from another
    DragShift version. Asks on a daemon thread, so start-up never waits for the bus."""
    import threading

    def check():
        from . import platform_linux
        v = platform_linux.extension_version()
        if v is None:
            print("GNOME Shell extension not available: no global pointer or screen capture "
                  "(install it, then log out and back in)", file=sys.stderr, flush=True)
        elif v != VERSION:
            print(f"warning: the GNOME Shell extension is from DragShift {v}, the app is "
                  f"{VERSION}; reinstall to update it", file=sys.stderr, flush=True)
        if v is not None and platform_linux.extension_refuses_us():
            print("the GNOME Shell extension only answers the DragShift app: start it with "
                  "`python -m dragshift` or the dragshift launcher", file=sys.stderr,
                  flush=True)

    t = threading.Thread(target=check, name="extension-version", daemon=True)
    t.start()
    return t


def send_to_running(payload: dict) -> bool:
    s = QLocalSocket()
    s.connectToServer(IPC_NAME)
    if not s.waitForConnected(300):
        return False
    s.write(json.dumps(payload).encode("utf-8"))
    s.flush()
    s.waitForBytesWritten(500)
    s.disconnectFromServer()
    return True


def log_dir() -> Path:
    """Folder that holds log.txt: %LOCALAPPDATA%, ~/Library/Logs, or $XDG_STATE_HOME on Linux."""
    if IS_MAC:
        return Path.home() / "Library" / "Logs" / "DragShift"
    if IS_LINUX:
        state = os.environ.get("XDG_STATE_HOME", "")
        # XDG spec: an unset, empty or relative value means the default.
        base = Path(state) if os.path.isabs(state) else Path.home() / ".local" / "state"
        return base / "DragShift"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DragShift"


_log_file = None   # log.txt once setup_logging() has opened it


def log_line(text: str) -> None:
    """One line into log.txt; to stderr when no log file is open."""
    try:
        print(text, file=_log_file or sys.stderr, flush=True)
    except (OSError, ValueError):  # closed or unwritable: logging must never stop start-up
        pass


def setup_logging():
    """Errors go to log_dir()/log.txt (pythonw / the .exe have no console)."""
    import faulthandler
    base = log_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / "log.txt"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(base / "log.old.txt")
    global _log_file
    f = _log_file = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n==== DragShift {VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    if sys.stderr is None or getattr(sys, "frozen", False) or sys.executable.lower().endswith("pythonw.exe"):
        sys.stdout = sys.stderr = f
    faulthandler.enable(f)

    def hook(t, v, tb):
        f.write("Unhandled: " + "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        setup_logging()
    except Exception:
        pass
    if IS_WIN:
        try:  # crisp taskbar/tray icon grouping
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DragShift.App")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qt_argv = argv
    if IS_LINUX:
        qpa = linux_qpa(os.environ, argv)
        if qpa:  # an argument, not QT_QPA_PLATFORM, so child processes (xdg-open…) don't inherit it
            qt_argv = [*argv, "-platform", qpa]
    qapp = DragShiftQApplication(qt_argv)
    if IS_MAC:
        mac_setup(qapp)
    if IS_LINUX:
        linux_setup(qapp)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(VERSION)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(make_icon())

    if "--selftest" in argv:
        return selftest()
    files = [os.path.abspath(a) for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    # already running? hand over and exit (instant: no second UI start-up)
    if send_to_running({"files": files} if files else {"show": "--quiet" not in argv}):
        return 0
    if IS_LINUX:
        check_extension_version()
    app = DragShiftApp(qapp)
    qapp.file_opened.connect(lambda f: app.queue_files([f]))
    if qapp.early_files:
        app.queue_files(list(qapp.early_files))
    if files:  # e.g. "Send to → DragShift" or drag onto the .exe
        QTimer.singleShot(120, lambda: app.queue_files(files))
    elif not app.settings.get("welcomed"):
        QTimer.singleShot(300, app.show_welcome)
    elif "--quiet" not in argv:
        QTimer.singleShot(400, lambda: app.toasts.info("DragShift is running", app.running_text()))
    return qapp.exec()


def selftest() -> int:
    """Used by CI on the frozen .exe: exercise every library and build the UI, then exit."""
    import tempfile
    from PIL import Image
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.png"
            Image.new("RGBA", (64, 48), (255, 100, 80, 255)).save(src)
            for t in convert.targets_for("image", ".png"):
                convert.convert(src, "image", t)
            pdf = convert.convert(src, "image", "PDF")[0]
            for t in convert.PDF_TARGETS:
                convert.convert(pdf, "pdf", t)
            (Path(d) / "n.txt").write_text("hello")
            docx = convert.convert(Path(d) / "n.txt", "text", "DOCX")[0]
            convert.convert(docx, "docx", "TXT")
            imaging.add_background(convert.open_image(src), imaging.Background())
            imaging.compress_pdf(pdf)
            imaging.protect_pdf(pdf, "pw")
            from . import liquid
            liquid.render(Image.new("RGB", (80, 80)), liquid.sdf_circle(80, 80, 40, 40, 30))
            menu = RadialMenu(lambda p: {"Convert": [Item("PNG")]}, lambda p: "1 KB")
            menu._load([str(src)])
            n = lambda *a, **k: None
            for w in (tools.CropWindow(str(src), n), tools.AddBgWindow(str(src), n), tools.EditWindow(str(src), n),
                      tools.AnnotateWindow(str(src), n), tools.RedactWindow(str(src), n),
                      tools.MetadataWindow(str(src), n), tools.CompressWindow([str(src)], n),
                      tools.ResizeWindow([str(src)], n), tools.ProtectWindow(str(pdf), n)):
                w.close()
            ToastStack().info("ok", "ok").dismiss()
        print("SELFTEST OK")
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
