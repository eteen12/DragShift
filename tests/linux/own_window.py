"""DragShift's own windows, for the own-window capture tests (Linux, isolated session only).

CaptureAreaExcludingCaller leaves out the windows of the process that calls it, so the
windows and the capture have to live in one process, like in the app. This program
shows them and captures through dragshift.platform_linux / dragshift.glass on request.
Tests start it inside the isolated session (`tests/linux/session.py`) through
`OwnWindows`:

    with OwnWindows(gnome_session, shell_helper, kind="solid", count=2) as own:
        own.move(0, 300, 300, 400, 300)              # window 0 to a stage rect
        im = own.capture(300, 300, 400, 300, exclude=True)

Window kinds:
- "solid": a frameless, stay-on-top tool window that takes no focus, like DragShift's
  toasts (toasts.ToastCard), filled with SOLID, a colour nothing else on screen has.
- "plain": the same colour in a frameless normal window (Mutter only minimises normal
  windows; under Xwayland a Qt.Tool window is a utility window).
- "glass": a real dragshift.window.GlassWindow (the tool windows' base class).

Commands are JSON lines on stdin, one JSON reply line each on stdout:
- {"cmd": "capture", "rect": [x, y, w, h], "exclude": bool, "out": path,
   "via": "capture" | "grab_screen"} -> {"ok", "ms", "source"}: platform_linux.capture(
   ..., exclude_self=exclude), or glass.grab_screen(..., below_window=exclude) (its
   source in glass.LAST_SOURCE). The image is saved to `out` as PNG.
- {"cmd": "bench", "rect": [...], "exclude": bool, "n": int} -> {"ms": [...]}
- {"cmd": "minimize", "index": i} -> {"ok": true}
The first line it prints is {"type": "ready", "pid", "platform", "titles"}.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOLID = (230, 0, 200)        # magenta: not in the wallpaper, Files or the shell's UI


# ---------------------------------------------------------------- test side
class OwnWindows:
    """Runs this program in a `Session`. `shell` is a ready ShellHelper."""

    def __init__(self, session, shell, kind: str = "solid", count: int = 1,
                 platform: str | None = None, timeout: float = 20.0):
        from tests.linux.probe_window import qt_platform
        self.session, self.shell = session, shell
        self.platform = platform or qt_platform(session)
        self.proc = session.popen(
            [sys.executable, "-m", "tests.linux.own_window", kind, str(count)],
            cwd=REPO, env={"QT_QPA_PLATFORM": self.platform}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            self.ready = self._read(timeout)
            self.pid = self.ready["pid"]
            self.windows = [shell.wait_for_window(title=t, timeout=timeout)
                            for t in self.ready["titles"]]
        except BaseException:
            self.close()
            raise

    def _read(self, timeout: float) -> dict:
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline() if r else ""
        if not line:
            self.proc.kill()
            raise AssertionError(f"own_window gave no reply ({self.proc.poll()}): "
                                 f"{self.proc.stderr.read()[-3000:]}")
        return json.loads(line)

    def cmd(self, timeout: float = 20.0, **command) -> dict:
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()
        return self._read(timeout)

    def move(self, index: int, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        """Put window `index` at a stage rect and wait until it is painted there. A
        window that was just mapped can still be placed by Mutter after a move, so the
        frame is checked again after the repaint."""
        for _ in range(5):
            self.shell.move_window(self.windows[index]["id"], x, y, w, h)
            time.sleep(0.4)              # the move is drawn and the client repainted
            frame = self.window(index)["frame"]
            if frame == (x, y, w, h):
                return frame
        raise AssertionError(f"own window {index} stays at {frame}, not {(x, y, w, h)}")

    def capture(self, x, y, w, h, exclude: bool, via: str = "capture"):
        """The capture made by this process, as a PIL RGB image (None when it failed),
        and the reply (ms, source)."""
        from PIL import Image
        out = self.session.root / "tmp" / f"own-{time.monotonic_ns()}.png"
        reply = self.cmd(cmd="capture", rect=[x, y, w, h], exclude=exclude, out=str(out),
                         via=via)
        if not reply["ok"]:
            return None, reply
        with Image.open(out) as im:
            return im.convert("RGB"), reply

    def window(self, index: int) -> dict:
        wid = self.windows[index]["id"]
        return next(w for w in self.shell.list_windows() if w["id"] == wid)

    def __enter__(self) -> "OwnWindows":
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
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass


# ---------------------------------------------------------------- the windows' process
def main(argv=None) -> int:
    from PySide6.QtCore import QSocketNotifier, Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    from dragshift import glass, platform_linux

    class Solid(QWidget):
        def __init__(self, plain=False):
            super().__init__(None, Qt.Window | Qt.FramelessWindowHint if plain else
                             Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                             | Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_ShowWithoutActivating, not plain)

        def paintEvent(self, _e):
            QPainter(self).fillRect(self.rect(), QColor(*SOLID))

    kind, count = (argv or sys.argv[1:])[:2]
    # replies go to the original stdout; anything the app prints goes to stderr
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    app = QApplication([sys.argv[0]])
    wins, titles = [], []
    for i in range(int(count)):
        if kind == "glass":
            from dragshift.window import GlassWindow
            w = GlassWindow("Own window", size=(420, 320))
        else:
            w = Solid(plain=kind == "plain")
            w.resize(300, 200)
        title = f"DragShift own window {os.getpid()}-{i}"
        w.setWindowTitle(title)
        w.show()
        wins.append(w)
        titles.append(title)

    def reply(**kw):
        out.write(json.dumps(kw) + "\n")
        out.flush()

    def grab(rect, exclude, via):
        if via == "grab_screen":
            return glass.grab_screen(*rect, below_window=int(exclude))
        return platform_linux.capture(*rect, exclude_self=exclude)

    def handle():
        line = sys.stdin.readline()
        if not line:
            app.quit()
            return
        c = json.loads(line)
        if c["cmd"] == "capture":
            t = time.perf_counter()
            im = grab(c["rect"], c["exclude"], c.get("via", "capture"))
            ms = (time.perf_counter() - t) * 1000
            if im is not None:
                im.save(c["out"])
            reply(ok=im is not None, ms=ms, source=glass.LAST_SOURCE)
        elif c["cmd"] == "bench":
            times = []
            for _ in range(c["n"]):
                t = time.perf_counter()
                im = platform_linux.capture(*c["rect"], exclude_self=c["exclude"])
                times.append((time.perf_counter() - t) * 1000)
                if im is None:
                    break
            reply(ms=times, ok=im is not None)
        elif c["cmd"] == "minimize":
            wins[c["index"]].showMinimized()
            reply(ok=True)
        else:
            reply(ok=False, error=f"unknown command {c['cmd']!r}")

    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Read)
    notifier.activated.connect(handle)
    reply(type="ready", pid=os.getpid(), platform=QApplication.platformName(), titles=titles)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
