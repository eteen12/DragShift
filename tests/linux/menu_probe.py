"""Menu probe for the display-backend tests (#10; Linux, isolated session only).

A 400x400 window with exactly the window flags of DragShift's radial menu
(dragshift/radial.py), shown and hidden on command, so tests can measure what each Qt
platform does with it: where it lands, whether it stays on top and steals the focus, what
a drop from Files delivers and what QScreen.grabWindow returns. Tests drive it through
`MenuProbe`:

    with MenuProbe(session, shell, "wayland", place=True) as menu:
        menu.show(800, 450)            # centred there, like RadialMenu.popup
        menu.frame()                   # where Mutter really put it (ListWindows)

Modes:
- place=False: show() is QWidget.move() + show() + raise_(), what radial.py does today.
  Under Xwayland (xcb) that is enough; a native Wayland client cannot place itself.
- place=True: show() first asks DragShift's shell extension to place the window
  (`platform_linux.place_window`, PlaceWindow in extension/.../dbus.xml).
- overlay=True: option (b) of #10, one fullscreen transparent window whose input region
  (QWidget.setMask) is just the menu square.

Commands (JSON lines on stdin): show {x, y} (the centre), place {x, y} (the top-left,
through the extension only), hide, grab {x, y, w, h, path}, quit. Log records (JSON lines): ready {platform, pid, dpr, screen, avail, flags},
shown {cmd_t, req, placed, qt_frame}, painted {cmd_t, qt_frame}, placed {placed}, hidden,
grab {null, size, black, ms}, dragEnter/drop {lx, ly, urls, formats, possible, proposed,
action}, dragLeave. Times are time.monotonic().

The window paints a magenta square with a green inside and 1-logical-pixel black lines
plus a line of text, so a screenshot shows where it is and whether it is drawn at the
monitor's scale (crisp) or upscaled (blurred).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIZE = 400
_ACTIONS = ("CopyAction", "MoveAction", "LinkAction")


# ---------------------------------------------------------------- probe process
def main(argv=None) -> int:
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QPoint, QRect, QSocketNotifier, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QRegion
    from PySide6.QtWidgets import QApplication, QWidget

    from dragshift import platform_linux
    from dragshift.radial import RadialMenu

    ap = argparse.ArgumentParser(prog="python tests/linux/menu_probe.py")
    ap.add_argument("--log", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--place", action="store_true")
    ap.add_argument("--overlay", action="store_true")
    a = ap.parse_args(argv)

    def name(action):
        return next((n for n in _ACTIONS if action == getattr(Qt.DropAction, n)), None)

    class Menu(QWidget):
        def __init__(self, flags):
            super().__init__(None)
            self.log = open(a.log, "a", buffering=1, encoding="utf-8")
            self.setWindowTitle(a.title)
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setAcceptDrops(True)
            self.centre = QPoint(SIZE // 2, SIZE // 2)
            self.paint_for = None
            if not a.overlay:
                self.resize(SIZE, SIZE)

        def write(self, **record):
            record["t"] = time.monotonic()
            self.log.write(json.dumps(record) + "\n")

        def qt_frame(self):
            f = self.frameGeometry()
            return [f.x(), f.y(), f.width(), f.height()]

        def paintEvent(self, _e):
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            r = QRect(self.centre.x() - SIZE // 2, self.centre.y() - SIZE // 2, SIZE, SIZE)
            p.fillRect(r, QColor(255, 0, 255))
            p.fillRect(r.adjusted(20, 20, -20, -20), QColor(0, 200, 0))
            p.setPen(QColor(0, 0, 0))
            for i in range(40, SIZE - 40, 10):
                p.drawLine(r.x() + i, r.y() + 40, r.x() + i, r.y() + 120)
            p.drawText(r.adjusted(40, 130, -40, -40), Qt.AlignLeft | Qt.AlignTop,
                       "DragShift 0123456789")
            p.end()
            if self.paint_for is not None:
                self.write(type="painted", cmd_t=self.paint_for, qt_frame=self.qt_frame())
                self.paint_for = None

        def command(self, c):
            cmd = c["cmd"]
            if cmd == "show":
                t = time.monotonic()
                self.paint_for = t
                x, y = c["x"], c["y"]
                placed = None
                if a.overlay:
                    scr = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
                    g = scr.geometry()
                    self.centre = QPoint(x - g.x(), y - g.y())
                    self.setMask(QRegion(self.centre.x() - SIZE // 2,
                                         self.centre.y() - SIZE // 2, SIZE, SIZE))
                    if not self.isVisible():
                        self.setGeometry(g)
                        self.showFullScreen()
                    self.update()
                else:
                    tl = QPoint(x - SIZE // 2, y - SIZE // 2)
                    if a.place:
                        placed = platform_linux.place_window(a.title, tl.x(), tl.y())
                    self.move(tl)
                    self.show()
                    self.raise_()
                self.write(type="shown", cmd_t=t, req=[x, y], placed=placed,
                           qt_frame=self.qt_frame())
            elif cmd == "place":
                placed = platform_linux.place_window(a.title, c["x"], c["y"])
                self.write(type="placed", placed=placed)
            elif cmd == "hide":
                self.hide()
                self.write(type="hidden")
            elif cmd == "grab":
                t = time.monotonic()
                shot = QGuiApplication.primaryScreen().grabWindow(0, c["x"], c["y"], c["w"], c["h"])
                ms = (time.monotonic() - t) * 1000
                img = shot.toImage()
                black = None
                if not img.isNull():
                    if c.get("path"):
                        img.save(c["path"])
                    px = [img.pixelColor(x, y) for y in range(0, img.height(), 8)
                          for x in range(0, img.width(), 8)]
                    black = sum(q.red() + q.green() + q.blue() < 15 for q in px) / len(px)
                self.write(type="grab", null=img.isNull(), size=[img.width(), img.height()],
                           black=black, ms=ms)
            elif cmd == "quit":
                QApplication.quit()

        def _drag(self, kind, e, full):
            e.setDropAction(Qt.CopyAction)      # what radial.py answers
            e.accept()
            if kind == "dragMove":
                return
            md = e.mimeData()
            record = dict(type=kind, lx=round(e.position().x()), ly=round(e.position().y()),
                          proposed=name(e.proposedAction()), action=name(e.dropAction()))
            if full:
                record.update(formats=list(md.formats()), urls=[u.toString() for u in md.urls()],
                              possible=[n for n in _ACTIONS
                                        if e.possibleActions() & getattr(Qt.DropAction, n)])
            self.write(**record)

        def dragEnterEvent(self, e):
            self._drag("dragEnter", e, True)

        def dragMoveEvent(self, e):
            self._drag("dragMove", e, False)

        def dropEvent(self, e):
            self._drag("drop", e, True)

        def dragLeaveEvent(self, _e):
            self.write(type="dragLeave")

    app = QApplication([sys.argv[0]])
    # DragShift's own menu flags, so the probe can never drift from radial.py.
    flags = RadialMenu(lambda paths: {}, lambda paths: "").windowFlags()
    menu = Menu(flags)
    scr = QGuiApplication.primaryScreen()
    menu.write(type="ready", platform=QApplication.platformName(), pid=os.getpid(),
               dpr=scr.devicePixelRatio(), screen=list(scr.geometry().getRect()),
               avail=list(scr.availableGeometry().getRect()), flags=int(flags.value))
    buf = b""

    def readable():
        nonlocal buf
        data = os.read(0, 65536)
        if not data:
            QApplication.quit()
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                menu.command(json.loads(line))

    notifier = QSocketNotifier(0, QSocketNotifier.Read)
    notifier.activated.connect(readable)
    return app.exec()


# ---------------------------------------------------------------- test side
def xcb_env() -> dict:
    """Extra environment for an xcb (Xwayland) Qt client. DRAGSHIFT_XCB_LIBS names a
    directory with libxcb-cursor.so.0 for machines that lack libxcb-cursor0 (Qt's xcb
    plugin refuses to load without it); see docs/linux/display-backend.md."""
    libs = os.environ.get("DRAGSHIFT_XCB_LIBS")
    return {"LD_LIBRARY_PATH": libs} if libs else {}


@functools.lru_cache(maxsize=None)
def _loads(env_items: tuple) -> bool:
    r = subprocess.run([sys.executable, "-I", "-c",
                        "from PySide6.QtGui import QGuiApplication; QGuiApplication(['p'])"],
                       env=dict(env_items), capture_output=True, timeout=30)
    return r.returncode == 0


def xcb_loads(session) -> bool:
    """Whether Qt's xcb plugin loads in the session (with xcb_env())."""
    env = session.env(QT_QPA_PLATFORM="xcb", **xcb_env())
    return _loads(tuple(sorted(env.items())))


class MenuProbe:
    """Runs the probe in a `Session`; `shell` is a ready ShellHelper."""

    def __init__(self, session, shell, platform: str, place: bool = False,
                 overlay: bool = False, title: str | None = None, timeout: float = 20.0):
        self.session, self.shell, self.platform = session, shell, platform
        self.title = title or f"DragShift menu probe {secrets.token_hex(3)}"
        self.log = session.root / "tmp" / f"menu-{secrets.token_hex(4)}.jsonl"
        env = {"QT_QPA_PLATFORM": platform, **(xcb_env() if platform == "xcb" else {})}
        cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--log", str(self.log),
               "--title", self.title]
        cmd += ["--place"] if place else []
        cmd += ["--overlay"] if overlay else []
        self.proc = session.popen(cmd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            self.ready = self.wait_for("ready", timeout=timeout)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "MenuProbe":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.send(cmd="quit")
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
        for f in (self.proc.stdin, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass

    def send(self, **command) -> None:
        self.proc.stdin.write((json.dumps(command) + "\n").encode())
        self.proc.stdin.flush()

    def records(self, *types: str) -> list[dict]:
        try:
            lines = self.log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        recs = [json.loads(ln) for ln in lines if ln.endswith("}")]
        return [r for r in recs if not types or r["type"] in types]

    def mark(self) -> int:
        return len(self.records())

    def wait_for(self, type_: str, after: int = 0, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for r in self.records()[after:]:
                if r["type"] == type_:
                    return r
            if self.proc.poll() is not None:
                raise AssertionError(f"menu probe exited ({self.proc.returncode}): "
                                     f"{self.proc.stderr.read().decode()[-2000:]}")
            if time.monotonic() > deadline:
                raise AssertionError(f"menu probe logged no {type_} in {timeout:.0f} s")
            time.sleep(0.01)

    def window(self) -> dict | None:
        """The probe's ListWindows record, or None while it is not mapped."""
        return next((w for w in self.shell.list_windows() if w["title"] == self.title), None)

    def frame(self) -> tuple[int, int, int, int] | None:
        w = self.window()
        return tuple(w["frame"]) if w else None

    def show(self, x: int, y: int, settle: float = 0.3) -> dict:
        """Show it centred on (x, y) as RadialMenu.popup does; returns the `painted`
        record plus `listed_ms`: time from the command until ListWindows first lists
        the window. Waits `settle` s more for anything that would move it later."""
        mark = self.mark()
        self.send(cmd="show", x=x, y=y)
        shown = self.wait_for("shown", mark)
        painted = self.wait_for("painted", mark)
        deadline = time.monotonic() + 5
        while self.window() is None:
            if time.monotonic() > deadline:
                raise AssertionError("menu probe never appeared in ListWindows")
            time.sleep(0.002)
        listed_ms = (time.monotonic() - shown["cmd_t"]) * 1000
        time.sleep(settle)
        return dict(painted, placed=shown["placed"], listed_ms=listed_ms,
                    paint_ms=(painted["t"] - painted["cmd_t"]) * 1000)

    def place(self, x: int, y: int) -> bool:
        """PlaceWindow from the probe's own process (top-left at x, y); its reply."""
        mark = self.mark()
        self.send(cmd="place", x=x, y=y)
        return self.wait_for("placed", mark)["placed"]

    def hide(self) -> None:
        mark = self.mark()
        self.send(cmd="hide")
        self.wait_for("hidden", mark)
        deadline = time.monotonic() + 5
        while self.window() is not None and time.monotonic() < deadline:
            time.sleep(0.01)

    def grab(self, x: int, y: int, w: int, h: int, path=None) -> dict:
        mark = self.mark()
        self.send(cmd="grab", x=x, y=y, w=w, h=h, path=str(path) if path else None)
        return self.wait_for("grab", mark)


if __name__ == "__main__":
    sys.exit(main())
