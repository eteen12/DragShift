"""DragShift's real glass windows and toasts in one process, for tests/linux/
test_window_glass.py (Linux, isolated session only).

The extension knows the caller by its bus connection, so the windows, their captures and
the GetOwnWindowFrame polls must come from one process, like in the app. This program
runs the app's own classes (tools.CropWindow + present(), toasts.ToastStack with a real
conversion) with the app's logging, and records every capture a LiquidBacking makes
(rect, source, time and the PIL image glass.grab_screen returned), so a test can compare
a backdrop with what is really behind the window.

Start it with `GlassApp(session, shell)` (cwd = repo, `python -m tests.linux.glass_app`).
Commands are JSON lines on stdin, one JSON reply line each on stdout:
- {"cmd": "crop", "path"} -> {"title"}: tools.CropWindow(path).present(), what
  DragShiftApp.open_window does.
- {"cmd": "state", "title"} -> the widget's state: visible, active, image (rendered
  backdrop size or None), qt_frame (Qt's frameGeometry), frame (from the extension),
  captures (how many), last {rect, below, source, ms}.
- {"cmd": "save", "title", "source", "glass"}: the last captured PIL image and the rendered
  backdrop (LiquidBacking.image) as PNGs.
- {"cmd": "toast", "src"} -> {"title"}: a card from ToastStack.new() that shows "working",
  converts `src` to JPG for real (convert.convert) and turns into the done card.
- {"cmd": "dismiss", "title"}, {"cmd": "close", "title"}
- {"cmd": "bench", "title", "n"} -> per-step times of n refreshes and n frame polls.
The first line is {"type": "ready", "pid", "platform"}.
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


# ---------------------------------------------------------------- test side
class GlassApp:
    def __init__(self, session, timeout: float = 20.0):
        self.session = session
        self.proc = session.popen([sys.executable, "-m", "tests.linux.glass_app"], cwd=REPO,
                                  env={"QT_QPA_PLATFORM": "wayland"}, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  bufsize=1)
        try:
            self.ready = self._read(timeout)
        except BaseException:
            self.close()
            raise

    @property
    def log_path(self) -> Path:
        return Path(self.session.env()["XDG_STATE_HOME"]) / "DragShift" / "log.txt"

    def log_lines(self, needle: str) -> list[str]:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return []
        return [ln for ln in text.splitlines() if needle in ln]

    def _read(self, timeout: float) -> dict:
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline() if r else ""
        if not line:
            self.proc.kill()
            raise AssertionError(f"glass_app gave no reply ({self.proc.poll()}): "
                                 f"{self.proc.stderr.read()[-3000:]}")
        return json.loads(line)

    def cmd(self, timeout: float = 30.0, **command) -> dict:
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()
        return self._read(timeout)

    def images(self, title: str):
        """(last captured source, rendered backdrop) as PIL RGB images (None if absent)."""
        from PIL import Image
        stem = self.session.root / "tmp" / f"glass-{time.monotonic_ns()}"
        reply = self.cmd(cmd="save", title=title, source=f"{stem}-src.png",
                         glass=f"{stem}-glass.png")
        out = []
        for key in ("source", "glass"):
            if reply.get(key):
                with Image.open(reply[key]) as im:
                    out.append(im.convert("RGB"))
            else:
                out.append(None)
        return out

    def __enter__(self) -> "GlassApp":
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


# ---------------------------------------------------------------- the app's process
def main() -> int:
    import gc
    import statistics

    from PySide6.QtCore import QEventLoop, QSocketNotifier, Qt, QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication

    from dragshift import app as dsapp
    from dragshift import convert, glass, platform_linux, report, toasts, tools

    out = os.fdopen(os.dup(1), "w")       # replies; anything the app prints goes to stderr
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    dsapp.setup_logging()                 # the session's XDG_STATE_HOME/DragShift/log.txt
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication([sys.argv[0]])
    dsapp.linux_setup(qapp)
    qapp.setQuitOnLastWindowClosed(False)

    widgets: dict[str, object] = {}
    records: dict[str, list[dict]] = {}
    capturing = []
    stack = toasts.ToastStack()

    real_grab, real_capture_at = glass.grab_screen, glass.LiquidBacking.capture_at

    def grab_screen(x, y, w, h, below_window=0):
        t = time.perf_counter()
        im = real_grab(x, y, w, h, below_window)
        rec = {"rect": [x, y, w, h], "below": below_window, "source": glass.LAST_SOURCE,
               "ms": (time.perf_counter() - t) * 1000, "image": im}
        if capturing:
            lst = records.setdefault(capturing[-1].windowTitle(), [])
            lst.append(rec)
            for old in lst[:-2]:
                old["image"] = None       # keep the last two images only
        return im

    def capture_at(self, rect):
        capturing.append(self.w)
        try:
            return real_capture_at(self, rect)
        finally:
            capturing.pop()

    glass.grab_screen = grab_screen
    glass.LiquidBacking.capture_at = capture_at

    def reply(**kw):
        out.write(json.dumps(kw) + "\n")
        out.flush()

    def spin(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def rect(r):
        return [r.x(), r.y(), r.width(), r.height()] if r is not None else None

    def state(title):
        w = widgets.get(title)
        if w is None:
            return {"exists": False}
        recs = records.get(title, [])
        last = recs[-1] if recs else None
        img = w.liquid.image
        return {"exists": True, "visible": w.isVisible(), "active": w.isActiveWindow(),
                "image": [img.width(), img.height()] if img is not None and not img.isNull()
                else None,
                "excluded": w.liquid.excluded, "size": [w.width(), w.height()],
                "qt_frame": rect(w.frameGeometry()), "frame": rect(getattr(w, "_frame", None)),
                "captures": len(recs),
                "last": {k: v for k, v in last.items() if k != "image"} if last else None}

    def bench(title, n):
        w = widgets[title]
        if hasattr(w, "life"):
            w.life.stop()                 # a card must outlive the benchmark
        res = {"poll": [], "gc": [], "capture": [], "render": [], "total": []}
        for _ in range(n):
            t = time.perf_counter()
            platform_linux.window_frame(w.windowTitle())
            res["poll"].append((time.perf_counter() - t) * 1000)
        done = []

        def changed():
            done.append(time.perf_counter())

        w.liquid.changed.connect(changed)
        for _ in range(n):
            done.clear()
            t0 = time.perf_counter()
            w.liquid.refresh()            # what a settled move or resize runs
            deadline = time.monotonic() + 5
            while not done and time.monotonic() < deadline:
                qapp.processEvents(QEventLoop.AllEvents, 10)
            if not done:
                break
            res["total"].append((done[0] - t0) * 1000)
            res["capture"].append(records[title][-1]["ms"])
            spin(30)
        w.liquid.changed.disconnect(changed)
        # the render on its own (pool thread), from the last capture
        from dragshift import liquid
        src = records[title][-1]["image"]
        for _ in range(n):
            t = time.perf_counter()
            sdf = liquid.sdf_rounded_rect(src.width, src.height, 0.5, 0.5, src.width - 0.5,
                                          src.height - 0.5, w.liquid.radius)
            liquid.render(src, sdf, w.liquid.style)
            res["render"].append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            gc.collect()
            res["gc"].append((time.perf_counter() - t) * 1000)
        return {k: {"median": statistics.median(v), "max": max(v), "n": len(v)}
                for k, v in res.items() if v}

    def handle():
        line = sys.stdin.readline()
        if not line:
            qapp.quit()
            return
        c = json.loads(line)
        try:
            if c["cmd"] == "crop":
                w = tools.CropWindow(c["path"], lambda *a, **k: None)
                widgets[w.windowTitle()] = w
                w.present()
                reply(title=w.windowTitle())
            elif c["cmd"] == "state":
                reply(**state(c["title"]))
            elif c["cmd"] == "save":
                w = widgets[c["title"]]
                recs = [r for r in records.get(c["title"], []) if r["image"] is not None]
                src = recs[-1]["image"] if recs else None
                saved = {}
                if src is not None:
                    src.save(c["source"])
                    saved["source"] = c["source"]
                if w.liquid.image is not None and not w.liquid.image.isNull():
                    w.liquid.image.save(c["glass"])
                    saved["glass"] = c["glass"]
                reply(**saved)
            elif c["cmd"] == "toast":
                card = stack.new()
                widgets[card.windowTitle()] = card
                card.working("Converting to JPG…", Path(c["src"]).name)
                spin(300)
                outs = convert.convert(c["src"], "image", "JPG")
                card.done(report.summarize("convert:JPG", [c["src"]], outs))
                reply(title=card.windowTitle())
            elif c["cmd"] == "dismiss":
                widgets.pop(c["title"]).dismiss()
                reply(ok=True)
            elif c["cmd"] == "close":
                widgets.pop(c["title"]).close()
                reply(ok=True)
            elif c["cmd"] == "bench":
                reply(**bench(c["title"], c.get("n", 10)))
            else:
                reply(error=f"unknown command {c['cmd']!r}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            reply(error=f"{type(e).__name__}: {e}")

    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Read)
    notifier.activated.connect(handle)
    reply(type="ready", pid=os.getpid(), platform=QApplication.platformName())
    code = qapp.exec()
    QThreadPool.globalInstance().waitForDone()
    return code


if __name__ == "__main__":
    sys.exit(main())
