"""Probe window for input-injection tests (Linux, isolated session only).

A frameless PySide6 window that logs every pointer and keyboard event it receives as JSON
lines, so tests can check what an injected event looks like to a real client. Tests start
it inside the isolated session (`tests/linux/session.py`) through `ProbeWindow`:

    with ProbeWindow(gnome_session, shell_helper) as probe:
        inject.click(shell_helper, *probe.center())
        probe.wait_for("mouseRelease")

It runs under Xwayland (`QT_QPA_PLATFORM=xcb`) when Qt can load the xcb plugin there,
and as a native Wayland client otherwise (Qt 6.5+ needs libxcb-cursor0 for xcb, which a
default Ubuntu 24.04 desktop does not have; the Docker image does).

Log records (one JSON object per line, flushed immediately):
- {"type": "ready", "xid" (0 unless xcb), "pid", "geometry": [x, y, w, h], "platform"}
- {"type": "mouseMove" | "mousePress" | "mouseRelease", "x", "y" (global as Qt sees it;
  a Wayland client cannot know its position, so there it equals lx, ly), "lx", "ly"
  (window-local), "button", "buttons", "modifiers"}
- {"type": "keyPress" | "keyRelease", "key" (Qt.Key name), "text", "modifiers",
  "autorepeat"}
- {"type": "text", "text": <line edit contents>} whenever the line edit changes.
Modifier and button names are Qt's ("ShiftModifier", "LeftButton").

The window also paints what it received (the stroke of every drag, press and release
dots, the last key), so a screenshot shows injected input.
"""
from __future__ import annotations

import argparse
import functools
import itertools
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

_MODIFIERS = ("ShiftModifier", "ControlModifier", "AltModifier", "MetaModifier",
              "KeypadModifier", "GroupSwitchModifier")
_BUTTONS = ("LeftButton", "RightButton", "MiddleButton", "BackButton", "ForwardButton")
_MOUSE = {QEvent.Type.MouseMove: "mouseMove", QEvent.Type.MouseButtonPress: "mousePress",
          QEvent.Type.MouseButtonRelease: "mouseRelease"}
_KEYS = {QEvent.Type.KeyPress: "keyPress", QEvent.Type.KeyRelease: "keyRelease"}


def _flags(value, names, enum):
    return [n for n in names if value & getattr(enum, n)]


def _key_name(key: int) -> str:
    try:
        return Qt.Key(key).name
    except ValueError:
        return f"0x{key:x}"


class Canvas(QWidget):
    """Draws the strokes, presses and releases the probe received."""

    def __init__(self):
        super().__init__()
        self.strokes: list[list[QPointF]] = []
        self.presses: list[QPointF] = []
        self.releases: list[QPointF] = []
        self.status = "waiting for input"
        self.setMinimumSize(200, 200)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#f4f1e8"))
        p.setPen(QPen(QColor("#c9c3b3"), 1, Qt.PenStyle.DashLine))
        for x in range(0, self.width(), 50):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 50):
            p.drawLine(0, y, self.width(), y)
        p.setPen(QPen(QColor("#1f5fbf"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        for stroke in self.strokes:
            for a, b in zip(stroke, stroke[1:]):
                p.drawLine(a, b)
        for points, color in ((self.presses, "#d03030"), (self.releases, "#2a9d4a")):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            for pt in points:
                p.drawEllipse(pt, 9, 9)
        p.setPen(QColor("#222222"))
        p.setFont(QFont("Sans", 13))
        p.drawText(self.rect().adjusted(12, 0, -12, -10),
                   Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, self.status)


class Probe(QWidget):
    def __init__(self, log_path: str, title: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self._log = open(log_path, "a", buffering=1, encoding="utf-8")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("TypeText goes here")
        self.edit.setFont(QFont("Sans", 16))
        self.edit.textChanged.connect(lambda t: self.write(type="text", text=t))
        self.canvas = Canvas()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(self.edit)
        lay.addWidget(self.canvas, 1)
        self._down = False

    def write(self, **record) -> None:
        record["t"] = round(time.monotonic(), 4)
        self._log.write(json.dumps(record) + "\n")

    def watch(self) -> None:
        """Log at the QWindow, where each platform event arrives exactly once (widget
        delivery propagates and would log some events twice)."""
        self.windowHandle().installEventFilter(self)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        kind = ev.type()
        if kind in _MOUSE:
            g, local = ev.globalPosition(), ev.position()
            self.write(type=_MOUSE[kind], x=round(g.x()), y=round(g.y()),
                       lx=round(local.x()), ly=round(local.y()),
                       button=(_flags(ev.button(), _BUTTONS, Qt.MouseButton) or [None])[0],
                       buttons=_flags(ev.buttons(), _BUTTONS, Qt.MouseButton),
                       modifiers=_flags(ev.modifiers(), _MODIFIERS, Qt.KeyboardModifier))
            self._paint_mouse(_MOUSE[kind], self.canvas.mapFromGlobal(g),
                              ev.buttons() != Qt.MouseButton.NoButton)
        elif kind in _KEYS:
            mods = _flags(ev.modifiers(), _MODIFIERS, Qt.KeyboardModifier)
            self.write(type=_KEYS[kind], key=_key_name(ev.key()), text=ev.text(),
                       modifiers=mods, autorepeat=ev.isAutoRepeat())
            self.canvas.status = f"{_KEYS[kind]} {_key_name(ev.key())} {'+'.join(mods)}"
            self.canvas.update()
        return False

    def _paint_mouse(self, kind: str, pos: QPointF, held: bool) -> None:
        c = self.canvas
        if kind == "mousePress":
            c.presses.append(pos)
            c.strokes.append([pos])
        elif kind == "mouseRelease":
            c.releases.append(pos)
            if c.strokes:
                c.strokes[-1].append(pos)
        elif held and c.strokes:
            c.strokes[-1].append(pos)
        c.status = f"{kind} ({round(pos.x())}, {round(pos.y())}) in canvas"
        c.update()


# ---------------------------------------------------------------- test side
@functools.lru_cache(maxsize=None)
def _xcb_loads(env_items: tuple) -> bool:
    env = dict(env_items)
    r = subprocess.run([sys.executable, "-I", "-c",
                        "from PySide6.QtGui import QGuiApplication; QGuiApplication(['p'])"],
                       env=dict(env, QT_QPA_PLATFORM="xcb"), capture_output=True, timeout=30)
    return r.returncode == 0


def qt_platform(session) -> str:
    """"xcb" when Qt's xcb plugin loads in the session (Xwayland), else "wayland"."""
    return "xcb" if _xcb_loads(tuple(sorted(session.env().items()))) else "wayland"


class ProbeWindow:
    """Runs the probe in a `Session` and reads its log. `shell` is a ready ShellHelper.

    platform: "xcb" or "wayland"; default qt_platform(session).
    """

    def __init__(self, session, shell, platform: str | None = None,
                 geometry=(200, 120, 900, 600), timeout: float = 20.0):
        self.session, self.shell = session, shell
        self.platform = platform or qt_platform(session)
        self.title = f"DragShift input probe {secrets.token_hex(3)}"
        self.log = session.root / "tmp" / f"probe-{secrets.token_hex(4)}.jsonl"
        self.proc = session.popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--log", str(self.log),
             "--title", self.title, "--geometry", ",".join(map(str, geometry))],
            env={"QT_QPA_PLATFORM": self.platform}, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        try:
            self.ready = self.wait_for("ready", timeout=timeout)
            self.window = shell.wait_for_window(title=self.title, timeout=timeout)
            self._settle(timeout)
        except BaseException:
            self.close()
            raise

    def _settle(self, timeout: float) -> None:
        """Wait until pointer events land where the window frame says they should. While
        the shell's map animation runs, the window is scaled and positions are off."""
        deadline = time.monotonic() + timeout
        for i in itertools.count():
            target = self.center(i % 2, 0)
            mark = self.mark()
            self.shell.move_pointer(*target)
            try:
                r = self.wait_for("mouseMove", after=mark, timeout=1)
                if self.screen_pos(r) == target:
                    return
            except AssertionError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"probe window never settled at {self.frame()}")
            time.sleep(0.1)

    def __enter__(self) -> "ProbeWindow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stderr.close()

    def events(self, *types: str) -> list[dict]:
        """Logged records so far, optionally only these types."""
        try:
            lines = self.log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        records = [json.loads(ln) for ln in lines if ln.endswith("}")]
        return [r for r in records if not types or r["type"] in types]

    def wait_for(self, type_: str, pred=lambda r: True, timeout: float = 5.0,
                 after: int = 0) -> dict:
        """The first record of `type_` (index >= `after` among all records) matching
        `pred`; raises AssertionError with the log tail on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            for r in self.events()[after:]:
                if r["type"] == type_ and pred(r):
                    return r
            if self.proc.poll() is not None:
                raise AssertionError(f"probe exited ({self.proc.returncode}): "
                                     f"{self.proc.stderr.read()[-2000:]}")
            if time.monotonic() > deadline:
                tail = "\n".join(map(json.dumps, self.events()[-15:]))
                raise AssertionError(f"probe logged no {type_} matching within "
                                     f"{timeout:.0f} s; last records:\n{tail}")
            time.sleep(0.05)

    def mark(self) -> int:
        """Number of records so far (pass as `after` to wait_for)."""
        return len(self.events())

    def frame(self) -> tuple[int, int, int, int]:
        """The window's current frame (x, y, w, h) on the stage (frameless: the client
        area)."""
        for w in self.shell.list_windows():
            if w["title"] == self.title:
                self.window = w
                return w["frame"]
        raise AssertionError(f"probe window {self.title!r} is gone")

    def screen_pos(self, record: dict) -> tuple[int, int]:
        """Stage coordinates of a mouse record: Qt's global position under xcb, the
        window frame plus the local position on Wayland."""
        if self.platform == "xcb":
            return record["x"], record["y"]
        x, y, _, _ = self.frame()
        return x + record["lx"], y + record["ly"]

    def center(self, dx: int = 0, dy: int = 0) -> tuple[int, int]:
        """A stage point inside the probe's canvas: its centre plus (dx, dy)."""
        x, y, w, h = self.frame()
        return x + w // 2 + dx, y + h // 2 + dy

    def focus(self) -> None:
        """Activate the probe and wait until it has the keyboard focus."""
        wid = self.window["id"]
        assert self.shell.activate_window(wid)
        deadline = time.monotonic() + 5
        while not any(w["id"] == wid and w["focused"] for w in self.shell.list_windows()):
            if time.monotonic() > deadline:
                raise AssertionError("probe window did not get the focus")
            time.sleep(0.05)


# ---------------------------------------------------------------- probe process
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.linux.probe_window")
    ap.add_argument("--log", required=True, help="JSON-lines event log (appended)")
    ap.add_argument("--title", default="DragShift input probe")
    ap.add_argument("--geometry", default="200,120,900,600", help="x,y,w,h")
    a = ap.parse_args(argv)
    x, y, w, h = (int(v) for v in a.geometry.split(","))
    app = QApplication([sys.argv[0]])
    probe = Probe(a.log, a.title)
    probe.setGeometry(x, y, w, h)
    probe.show()
    probe.watch()
    probe.edit.setFocus()
    g = probe.geometry()
    platform = QApplication.platformName()
    probe.write(type="ready", xid=int(probe.winId()) if platform == "xcb" else 0,
                pid=os.getpid(),
                geometry=[g.x(), g.y(), g.width(), g.height()],
                platform=platform)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
