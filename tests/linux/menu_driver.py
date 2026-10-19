"""DragShift's real radial menu on command, for the placement tests (#34; Linux, isolated
session only).

Runs `dragshift.radial.RadialMenu` as the app does (native Wayland chosen by
`app.linux_qpa`, `app.linux_setup`) and calls its `popup()` when told, so the tests see
exactly what the app does: the extension's PlaceWindow by the menu's title, the clamp to
the monitor and the glass backdrop from the extension's capture. It is started as
`python -m tests.linux.menu_driver` from the repo root, a command line the extension's
caller check (#93) lets in inside a test session.

Commands (JSON lines on stdin): popup {x, y} (centre; both left out = popup() with no
position, i.e. at the extension's pointer; optional `paths`: click mode for those files,
with a 4-petal ring; optional `frost`: no backdrop at all, the painted frost fallback),
hide, quit. Log records (JSON lines): ready {pid, platform, screens, areas (each screen's
work area from the extension, or null)}, shown {tl, visible, title, places, captures,
source, backdrop, capture, glass}, hidden. `places` and `captures` are the extension calls
popup() made (PlaceWindow title/x/y/reply, CaptureArea x/y/w/h); `backdrop` is the PNG of
the raw backdrop the menu renders its glass from (after the cursor inpaint), `capture` the
PNG of the extension's capture as it came back (before it), `glass` the PNG of the
rendered liquid glass on screen (#39). The menu's own lines (`menu: ...`) go to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tests.linux.menu_probe import MenuProbe

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.linux.menu_driver")
    ap.add_argument("--log", required=True)
    a = ap.parse_args(argv)

    from PySide6.QtCore import QPoint, QSocketNotifier, Qt, QThreadPool
    from PySide6.QtWidgets import QApplication

    from dragshift import app, glass, platform_linux
    from dragshift.radial import RadialMenu

    log = open(a.log, "a", buffering=1, encoding="utf-8")

    def write(**record):
        record["t"] = time.monotonic()
        log.write(json.dumps(record) + "\n")

    # Record the extension calls popup() makes, and still make them.
    calls = {"places": [], "captures": []}
    place, capture = platform_linux.place_window, platform_linux.capture

    def place_window(title, x, y):
        reply = place(title, x, y)
        calls["places"].append([title, x, y, reply])
        return reply

    last = {}

    def capture_(x, y, w, h, exclude_self=False):
        calls["captures"].append([x, y, w, h, exclude_self])
        last["capture"] = im = capture(x, y, w, h, exclude_self)
        return im

    platform_linux.place_window, platform_linux.capture = place_window, capture_
    grab_screen = glass.grab_screen

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qpa = app.linux_qpa(os.environ, sys.argv)
    qapp = QApplication([sys.argv[0], *(["-platform", qpa] if qpa else [])])
    app.linux_setup(qapp)
    from dragshift.radial import Item
    ring = [Item(t) for t in ("PNG", "JPG", "WEBP", "PDF")]
    menu = RadialMenu(lambda paths: {"Convert": ring} if paths else {},
                      lambda paths: f"{len(paths)} file(s)")
    screens = [s.geometry() for s in qapp.screens()]
    write(type="ready", pid=os.getpid(), platform=qapp.platformName(),
          screens=[list(g.getRect()) for g in screens],
          areas=[platform_linux.work_area(g.center().x(), g.center().y()) for g in screens])
    shots = Path(a.log).with_suffix("")
    n = 0

    def command(c):
        nonlocal n
        cmd = c["cmd"]
        if cmd == "popup":
            for v in calls.values():
                v.clear()
            last.clear()
            glass.grab_screen = (lambda *a, **k: None) if c.get("frost") else grab_screen
            menu.popup(QPoint(c["x"], c["y"]) if "x" in c else None, c.get("paths"))
            glass.grab_screen = grab_screen
            QThreadPool.globalInstance().waitForDone(5000)   # the glass renders
            QApplication.processEvents()                     # ... and is handed over
            n += 1
            files = {}
            ring = len(menu.items) or 4
            for key, im in (("backdrop", menu._raw_backdrop), ("capture", last.get("capture")),
                            ("glass", menu.liquid.get(ring))):
                if im is not None and not (key == "glass" and im.isNull()):
                    files[key] = f"{shots}-{key}-{n}.png"
                    im.save(files[key])
            write(type="shown", tl=[menu.x(), menu.y()], visible=menu.isVisible(),
                  title=menu.windowTitle(), places=calls["places"][:],
                  captures=calls["captures"][:], source=glass.LAST_SOURCE,
                  backdrop=files.get("backdrop"), capture=files.get("capture"),
                  glass=files.get("glass"))
        elif cmd == "hide":
            menu.close_menu()
            write(type="hidden")
        elif cmd == "quit":
            QApplication.quit()

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
                command(json.loads(line))

    notifier = QSocketNotifier(0, QSocketNotifier.Read)
    notifier.activated.connect(readable)
    return qapp.exec()


class MenuDriver(MenuProbe):
    """Runs the driver in a `Session`; `shell` is a ready ShellHelper. The probe's
    plumbing (send, records, wait_for, close) is reused; `window()` is the real menu's
    ListWindows record, found by its title and this process's pid."""

    def __init__(self, session, shell, timeout: float = 20.0):
        import secrets
        import subprocess
        from dragshift.radial import MENU_TITLE
        self.session, self.shell, self.platform = session, shell, "wayland"
        self.title = MENU_TITLE
        self.log = session.root / "tmp" / f"driver-{secrets.token_hex(4)}.jsonl"
        self.out = self.log.with_suffix(".out")
        with open(self.out, "wb") as out:
            self.proc = session.popen([sys.executable, "-m", "tests.linux.menu_driver",
                                       "--log", str(self.log)],
                                      cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                                      stdin=subprocess.PIPE, stdout=out,
                                      stderr=subprocess.PIPE)
        try:
            self.ready = self.wait_for("ready", timeout=timeout)
        except BaseException:
            self.close()
            raise

    def area(self, x: int, y: int):
        """(monitor, work area or None) of the screen containing (x, y), as the app sees
        them."""
        for mon, area in zip(self.ready["screens"], self.ready["areas"]):
            mx, my, mw, mh = mon
            if mx <= x < mx + mw and my <= y < my + mh:
                return tuple(mon), tuple(area) if area else None
        raise AssertionError(f"({x}, {y}) is on no screen: {self.ready['screens']}")

    def window(self) -> dict | None:
        return next((w for w in self.shell.list_windows() if w["title"] == self.title
                     and w["pid"] == self.ready["pid"]), None)

    def popup(self, x: int | None = None, y: int | None = None, settle: float = 0.3,
              **opts) -> dict:
        """popup() centred on (x, y), or at the pointer when both are None (`opts`: paths,
        frost); returns the `shown` record once the shell lists the menu, plus `frame` (its
        frame then)."""
        mark = self.mark()
        self.send(cmd="popup", **({} if x is None else {"x": x, "y": y}), **opts)
        shown = self.wait_for("shown", mark)
        deadline = time.monotonic() + 5
        while self.window() is None:
            if time.monotonic() > deadline:
                raise AssertionError("the menu never appeared in ListWindows")
            time.sleep(0.01)
        time.sleep(settle)                  # anything that would move it later
        return dict(shown, frame=self.frame())

    def hide(self) -> None:
        mark = self.mark()
        self.send(cmd="hide")
        self.wait_for("hidden", mark)
        deadline = time.monotonic() + 5
        while self.window() is not None and time.monotonic() < deadline:
            time.sleep(0.01)


if __name__ == "__main__":
    sys.exit(main())
