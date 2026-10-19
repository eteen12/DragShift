"""End-to-end test on Linux (Ubuntu 24.04, GNOME Shell 46, Wayland), the twin of
demo/e2e_windows.py.

It boots a throwaway headless GNOME Shell session (tests/linux/session.py: its own HOME,
XDG dirs, TMPDIR, runtime dir and buses; nothing touches your real desktop) with DragShift's
extension and the test-helper extension, starts the real app from this Python, makes the
sample files in the session HOME and opens them in Files. Every scenario is a real mouse
drag of a file injected into that session: press on the icon → move → press Shift → the
menu opens → into the menu → (Shift tap for the Tools ring) → onto a petal → release →
(drive the tool window) → verify the output on disk and that the dragged files are
untouched.

    python demo/e2e_linux.py                          # every scenario
    python demo/e2e_linux.py --only "JPG → PNG"       # one (repeat --only for more)
    python demo/e2e_linux.py --list                   # the scenario names
    python demo/e2e_linux.py --record --out /tmp/e2e  # + e2e_linux.mp4 (or .webp)

Writes e2e_linux_out/ (or --out): results.json, results.md, log.txt, app_log.txt (the
app's output, then its log.txt), shots/ and with --record a video. Exit code 1 when a
scenario is not PASS or none ran.

Scenarios: a `Scenario(...)` takes the arguments of e2e_windows.run(), with
`where="nautilus"` (or "desktop" for the D-* files on the DING desktop); tool windows get a
`tool` driver, called right after the drop. The runner runs SCENARIOS at the bottom of this
file, then the SCENARIOS list of every demo/e2e_linux_*.py module, sorted by file name
(`from e2e_linux import Scenario, C, inject, ...` works there). A module that fails to import,
has no SCENARIOS list or repeats a scenario name stops the run."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __name__ == "__main__":   # scenario modules import `e2e_linux`: give them this module
    sys.modules.setdefault("e2e_linux", sys.modules[__name__])

from e2e_common import ROOT, SOURCES, img_ok, make_files, new_files, ring_index, safe, snapshot, v_format

if not sys.platform.startswith("linux"):
    sys.exit("e2e_linux.py runs on Linux only (see e2e_windows.py and e2e_mac.py)")

from dragshift.app import LINUX_APP_ID, DragShiftApp  # noqa: E402
from dragshift.platform_linux import BUS_NAME  # noqa: E402
from dragshift.radial import MENU_TITLE, R_IN, R_OUT, SIZE, angles  # noqa: E402
from dragshift.toasts import TITLE as TOAST_TITLE  # noqa: E402
from tests.linux import inject  # noqa: E402
from tests.linux.files import Files  # noqa: E402
from tests.linux.session import Session, SessionError, missing_tools  # noqa: E402
from tests.linux.shell import ShellHelper  # noqa: E402
from tests.linux.widgets import Widgets, WidgetsError  # noqa: E402

OUT = ROOT / "e2e_linux_out"
EXTENSION = ROOT / "extension"
UUID = "dragshift@eteen12.github.io"
R_PETAL = (R_IN + R_OUT) / 2     # 109, as in e2e_windows.py
FOLLOWING = "trigger: following the extension's pointer state"
TOWARD = {"nautilus": (1, 0.3), "desktop": (-1, -0.6)}   # from the icon into free space
MENU_WAIT = 3.0                  # s for the menu window after Shift
APP_START = 30.0                 # s for the app to follow the extension
SHIFT = inject.KEYSYMS["Shift_L"]

RESULTS: list[dict] = []
T0 = time.time()
LOG = None


@dataclass
class Scenario:
    """One row of the results table; the arguments of e2e_windows.run()."""
    name: str
    where: str                   # "nautilus" (EXP in a Files window) or "desktop" (DESK, D-*)
    files: list[str]             # dragged: the last one, after selecting the others
    ring: str                    # "Convert" or "Tools"
    label: str                   # the petal
    verify: Callable             # verify(outs, srcs) -> detail; raise to fail
    tool: Callable | None = None  # drives the tool window after the drop
    select_extra: bool = False   # Ctrl+click every file first (multi-select)


# ------------------------------------------------------------------ log
def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if LOG:
        LOG.write(f"[{time.time() - T0:7.2f}] {msg}\n")
        LOG.flush()


class Ctx:
    """The running session, its helper and the app."""
    session: Session
    shell: ShellHelper
    files: Files
    app: subprocess.Popen
    app_out: Path
    exp: Path        # the Files folder with the sample files
    desk: Path       # the session's DING desktop
    stash: Path      # outputs are moved here between scenarios


C = Ctx()


def app_text() -> str:
    try:
        return C.app_out.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait(fn, timeout: float, step: float = 0.05):
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v or time.monotonic() > end:
            return v
        time.sleep(step)


def shot(name: str) -> Path | None:
    """A screenshot of the session into OUT/shots (the helper writes inside the session)."""
    try:
        p = C.shell.screenshot(C.session.root / "tmp" / "shot.png")
        dest = OUT / "shots" / f"{name}.png"
        shutil.move(str(p), dest)
        return dest
    except Exception as e:
        log("screenshot failed:", e)
        return None


# ------------------------------------------------------------------ session and app
def build_extension(tmp: Path) -> Path:
    """extension/build.sh into tmp, unpacked into a directory named by the UUID."""
    r = subprocess.run(["sh", str(EXTENSION / "build.sh"), str(tmp)], capture_output=True,
                       text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"extension/build.sh failed: {r.stderr.strip()}")
    d = tmp / UUID
    with zipfile.ZipFile(r.stdout.strip()) as z:
        z.extractall(d)
    return d


def extension_on_bus() -> bool:
    r = C.session.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                       "--object-path", "/org/freedesktop/DBus",
                       "--method", "org.freedesktop.DBus.NameHasOwner", BUS_NAME])
    return r.stdout.strip() == "(true,)"


def start_app() -> subprocess.Popen:
    """DragShift from this Python, the way a user starts it, in the session (no Welcome)."""
    conf = Path(C.session.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")
    with open(C.app_out, "w") as f:
        proc = C.session.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                               env={"PYTHONPATH": str(ROOT)}, stdout=f,
                               stderr=subprocess.STDOUT)
    if not wait(lambda: FOLLOWING in app_text() or proc.poll() is not None, APP_START, 0.1) \
            or proc.poll() is not None:
        raise RuntimeError(f"DragShift did not start following the extension:\n{app_text()[-2000:]}")
    return proc


def stop_app():
    p = getattr(C, "app", None)
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def copy_app_log():
    """app_log.txt: the app's own output (where `menu:`/`trigger:` lines go), then log.txt."""
    try:
        state = Path(C.session.env()["XDG_STATE_HOME"]) / "DragShift" / "log.txt"
        with open(OUT / "app_log.txt", "a", encoding="utf-8") as f:
            f.write("\n==== $XDG_STATE_HOME/DragShift/log.txt ====\n")
            f.write(state.read_text(encoding="utf-8", errors="replace") if state.exists()
                    else "(none)\n")
    except Exception as e:
        log("app log copy failed:", e)


# ------------------------------------------------------------------ windows
def menus() -> list[dict]:
    """DragShift's menu windows, by title and app id (never by pid: with the launcher the
    app is the shell's own client, #95)."""
    return [w for w in C.shell.list_windows()
            if w["title"] == MENU_TITLE and w["wm_class"] == LINUX_APP_ID
            and tuple(w["frame"][2:]) == (SIZE, SIZE)]


def toasts() -> list[dict]:
    prefix = TOAST_TITLE.format("")
    return [w for w in C.shell.list_windows()
            if w["title"].startswith(prefix) and w["wm_class"] == LINUX_APP_ID]


def settled_menu_frame(timeout: float = 2.0) -> tuple:
    """The menu's frame once Mutter has placed it (the first record can be provisional)."""
    end = time.monotonic() + timeout
    last, stable = None, 0
    while True:
        m = menus()
        if not m:
            raise AssertionError("the menu closed")
        frame = tuple(m[-1]["frame"])
        stable = stable + 1 if frame == last else 0
        last = frame
        if stable >= 3 or time.monotonic() > end:
            return frame
        time.sleep(0.03)


def centre(frame) -> tuple[int, int]:
    return frame[0] + SIZE // 2, frame[1] + SIZE // 2


def petal_xy(frame, n: int, i: int, r=R_PETAL) -> tuple[int, int]:
    """Stage point in the middle of petal i of n, from the menu's own geometry."""
    a0, a1 = angles(n, i)
    m = math.radians((a0 + a1) / 2)
    x, y = centre(frame)
    return round(x + r * math.cos(m)), round(y - r * math.sin(m))


# ------------------------------------------------------------------ tool windows (#61)
# A tool driver (Scenario.tool) gets the glass window with tool_window("Compress"), then
# clicks its controls by label with injected input:
#
#     w = tool_window("Compress")
#     w.click("Target size", checked=True)
#     w.click("200 KB", checked=True)
#     w.click("Compress", primary=True)      # the window then closes and saves
#
# Controls are found over AT-SPI (tests/linux/widgets.py): Qt exports its tree on the
# session's accessibility bus once toolkit accessibility is on, which the first
# tool_window() turns on. A checkable QPushButton is a "check box" there, a QLineEdit a
# "text" (or "password text"), the image canvas (ImageView) a "filler". Their boxes are
# relative to the window, so the frame from ListWindows is added. Without AT-SPI only the
# primary button still works, through Enter (ToolWindow._enter), and the canvas comes from
# e2e_windows._canvas's heuristic.
TOOL_WAIT = 12.0                 # s for a tool window to appear after the drop
GLASS_SUFFIX = " — DragShift"    # GlassWindow titles: "<title> — DragShift", "<title> (2) — …"
TEXT_ROLES = ("text", "password text", "entry")
_WIDGETS: dict = {}              # "w": the Widgets client once accessibility is on, or None


def glass_windows() -> list[dict]:
    """DragShift's glass windows (tools, Settings, Welcome), by title and app id."""
    return [w for w in C.shell.list_windows()
            if w["wm_class"] == LINUX_APP_ID and w["title"].endswith(GLASS_SUFFIX)]


def widgets() -> Widgets | None:
    """The AT-SPI client, with toolkit accessibility turned on in the session (once), or
    None when the session has no accessibility bus."""
    if "w" not in _WIDGETS:
        w = Widgets(C.session)
        try:
            w.enable()
            _WIDGETS["w"] = w
        except WidgetsError as e:
            log("AT-SPI unavailable, tool drivers fall back to the keyboard:", e)
            _WIDGETS["w"] = None
    return _WIDGETS["w"]


class ToolWin:
    """A DragShift glass window being driven: its ListWindows record and its widgets."""

    def __init__(self, rec: dict):
        self.rec = rec

    @property
    def title(self) -> str:
        return self.rec["title"]

    @property
    def frame(self) -> tuple:
        return tuple(self.rec["frame"])

    def is_open(self) -> bool:
        return any(w["id"] == self.rec["id"] for w in C.shell.list_windows())

    def items(self) -> list[dict]:
        """The showing widgets, with `box` in stage coordinates (x, y, w, h)."""
        w = widgets()
        if w is None:
            raise AssertionError(f"no AT-SPI: can't find the controls of {self.title!r}")
        x0, y0 = self.frame[:2]
        out = []
        for it in w.find(self.title):
            x, y, bw, bh = it["box"]
            out.append(dict(it, box=(x0 + x, y0 + y, bw, bh)))
        return out

    def find(self, name: str, role: str | None = None, timeout: float = 3.0) -> dict:
        """The first showing, enabled widget named exactly `name` (and of `role`)."""
        end = time.monotonic() + timeout
        while True:
            found = [it for it in self.items() if it["name"] == name and it["enabled"]
                     and (role is None or it["role"] == role)]
            if found:
                return found[0]
            if time.monotonic() > end:
                raise AssertionError(f"no {role or 'widget'} {name!r} in {self.title!r}")
            time.sleep(0.2)

    def activate(self):
        """Focus the window (the edge case where present() didn't, #40)."""
        if not next((w["focused"] for w in C.shell.list_windows() if w["id"] == self.rec["id"]),
                    False):
            C.shell.activate_window(self.rec["id"])
            wait(lambda: any(w["focused"] for w in C.shell.list_windows()
                             if w["id"] == self.rec["id"]), 2)

    def click(self, name: str, role: str | None = None, checked: bool | None = None,
              primary: bool = False, pause: float = 0.3):
        """Glide to the widget named `name` and click it. checked: wait until a toggle
        shows that state. primary: the footer's primary button; without AT-SPI it is
        pressed with Enter instead."""
        self.activate()
        if primary:
            self.shot()
        if primary and widgets() is None:
            inject.press(C.shell, "Return")
            time.sleep(pause)
            return
        it = self.find(name, role or ("push button" if primary else None))
        x, y, w, h = it["box"]
        inject.click(C.shell, x + w // 2, y + h // 2, dur=0.25)
        if checked is not None and not wait(
                lambda: any(i["name"] == name and i["checked"] == checked for i in self.items()), 2,
                0.1):
            raise AssertionError(f"{name!r} in {self.title!r} did not become "
                                 f"{'checked' if checked else 'unchecked'}")
        time.sleep(pause)

    def shot(self):
        """Evidence: shots/NN_<title>_tool.png, the window as it is about to save (click()
        takes it before the primary button)."""
        shot(f"{len(RESULTS) + 1:02d}_{safe(self.title.removesuffix(GLASS_SUFFIX))}_tool")

    def canvas(self) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) of the image canvas: the largest "filler" (ImageView), or
        e2e_windows._canvas's guess from the frame without AT-SPI."""
        if widgets() is not None:
            fill = [it for it in self.items() if it["role"] == "filler"]
            if fill:
                x, y, w, h = max(fill, key=lambda it: it["box"][2] * it["box"][3])["box"]
                return x, y, x + w, y + h
        x, y, w, h = self.frame
        return x + 60, y + 80, x + w - 60, y + int(h * 0.6)

    def drag(self, start: tuple[int, int], end: tuple[int, int], dur: float = 0.5):
        """Press at `start`, glide to `end`, release (a stroke on the canvas)."""
        self.activate()
        inject.glide(C.shell, *start, dur=0.25)
        C.shell.button(1, True)
        try:
            time.sleep(0.05)
            inject.glide(C.shell, *end, dur=dur)
            time.sleep(0.05)
        finally:
            C.shell.button(1, False)
        time.sleep(0.2)

    def type_into(self, index: int, text: str):
        """Click the index-th text field (tree order) and type `text` (#62: Protect)."""
        fields = [it for it in self.items() if it["role"] in TEXT_ROLES]
        if len(fields) <= index:
            raise AssertionError(f"{self.title!r} has {len(fields)} text field(s)")
        x, y, w, h = fields[index]["box"]
        inject.click(C.shell, x + w // 2, y + h // 2, dur=0.25)
        time.sleep(0.15)
        inject.type_text(C.shell, text)

    def wait_closed(self, timeout: float = 5.0):
        """The window went away (a tool closes itself when it starts saving)."""
        if not wait(lambda: not self.is_open(), timeout, 0.1):
            raise AssertionError(f"{self.title!r} is still open")


def tool_window(title: str, timeout: float = TOOL_WAIT) -> ToolWin:
    """The glass window titled `title` (e.g. "Compress", "Crop Image") once Mutter has
    placed it, it is focused and (with AT-SPI) its widgets are exported."""
    want = re.compile(rf"{re.escape(title)}( \(\d+\))?{re.escape(GLASS_SUFFIX)}")
    found = wait(lambda: [w for w in glass_windows() if want.fullmatch(w["title"])], timeout, 0.1)
    if not found:
        raise AssertionError(f"no {title!r} window within {timeout:.0f} s "
                             f"(windows: {[w['title'] for w in C.shell.list_windows()]})")
    win = ToolWin(found[-1])
    last, stable, end = None, 0, time.monotonic() + 2.0       # Mutter's placement settles
    while stable < 3 and time.monotonic() < end:
        rec = next((w for w in glass_windows() if w["id"] == win.rec["id"]), None)
        if rec is None:
            raise AssertionError(f"{win.title!r} closed")
        stable = stable + 1 if tuple(rec["frame"]) == last else 0
        last, win.rec = tuple(rec["frame"]), rec
        time.sleep(0.05)
    win.activate()
    if widgets() is not None:
        end = time.monotonic() + 5.0
        while True:          # Qt registers on the bus a moment after accessibility is on
            try:
                if win.items():
                    break
            except WidgetsError:
                if time.monotonic() > end:
                    raise
            time.sleep(0.2)
    return win


def close_tool_windows():
    """Close every DragShift glass window left open (after a failure): Escape in it, then
    the ✕ button's AT-SPI action."""
    for rec in glass_windows():
        win = ToolWin(rec)
        try:
            C.shell.activate_window(rec["id"])
            time.sleep(0.2)
            inject.press(C.shell, "Escape")
            if wait(lambda: not win.is_open(), 2, 0.1):
                continue
            if widgets() is not None:
                widgets().press(win.title, "✕")
            if not wait(lambda: not win.is_open(), 2, 0.1):
                log(f"could not close {win.title!r}")
        except Exception as e:
            log(f"closing {win.title!r} failed:", e)


# ------------------------------------------------------------------ sources
def folder_of(where: str) -> Path:
    return C.exp if where == "nautilus" else C.desk


def prefix_of(folder: Path) -> str:
    return "" if folder == C.exp else "D-"


def stash(folder: Path, before: set[str]):
    """Move outputs out of the way so the folder only holds the sources again."""
    C.stash.mkdir(exist_ok=True)
    for n in sorted(snapshot(folder) - before, key=len, reverse=True):
        p = folder / n
        try:
            if p.is_file():
                shutil.move(str(p), C.stash / f"{int(time.time() * 1000)}_{p.name}")
            elif p.is_dir():
                p.rmdir()
        except Exception:
            pass


def restore_sources(folder: Path):
    """Put back sample files a failed drag moved away, and drop stray copies elsewhere, so
    one failure can't cascade into the rest of the run."""
    prefix = prefix_of(folder)
    missing = [n for n in SOURCES if not (folder / (prefix + n)).exists()]
    if missing:
        log("restoring sources:", missing)
        make_files(folder, prefix)
        time.sleep(1.0)
    other = C.desk if folder == C.exp else C.exp
    for n in SOURCES:   # a copy a failed drag dropped into the other place
        (other / (prefix + n)).unlink(missing_ok=True)


def fingerprint(paths: list[Path]) -> dict:
    return {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}


def check_untouched(before: dict):
    for p, (digest, mtime) in before.items():
        assert p.exists(), f"{p.name} was moved or deleted"
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, f"{p.name} changed"
        assert p.stat().st_mtime_ns == mtime, f"{p.name} was rewritten"


def ensure_view(where: str):
    """The Files window on EXP is open (reopened if it went away); the desktop has its
    files and no result cards over DING's icon column."""
    if where == "nautilus":
        win = C.files.windows.get(C.exp.resolve())
        if not win or win["id"] not in {w["id"] for w in C.shell.list_windows()}:
            log("opening Files on", C.exp)
            C.files.open_folder(C.exp)
        else:
            C.shell.activate_window(win["id"])
    else:
        wait(lambda: not toasts(), 15, 0.2)


# ------------------------------------------------------------------ the drag
def select(where: str, names: list[str]):
    """Click the first file, Ctrl+click the others, like a user."""
    inject.click(C.shell, *C.files.icon_center(names[0], where))
    for n in names[1:]:
        time.sleep(0.3)
        with inject.hold_key(C.shell, "Control_L"):
            inject.click(C.shell, *C.files.icon_center(n, where))
    time.sleep(0.8)    # longer than the double-click time: the drag's press opens nothing


def press_and_move(start, toward):
    """Button down on the icon, 2 px steps (slowly over the first 12 px, where the toolkit
    starts its drag) to 72 px, past the watcher's 20 px threshold, then Shift (#31, #35)."""
    x0, y0 = inject.glide(C.shell, *start, dur=0.25)
    C.shell.button(1, True)
    time.sleep(0.15)
    dx, dy = toward
    for k in range(1, 37):
        C.shell.move_pointer(x0 + dx * 2 * k, y0 + dy * 2 * k)
        time.sleep(0.02 if k <= 6 else 0.008)
    C.shell.key(SHIFT, True)


def shift_drag(name: str, where: str, files: list[str], ring: str, label: str,
               select_extra: bool, mark: int):
    """The real Shift+drag onto the petal; returns the app's output for the gesture."""
    gesture = lambda: app_text()[mark:]   # noqa: E731
    paths = [str(folder_of(where) / f) for f in files]
    ring_i, idx, n = ring_index(paths, ring, label)
    names = list(DragShiftApp.build_rings(None, paths))
    if select_extra:
        select(where, files)
    press_and_move(C.files.icon_center(files[-1], where), TOWARD[where])
    if not wait(menus, MENU_WAIT, 0.01):
        raise AssertionError(f"the menu did not open within {MENU_WAIT:.0f} s")
    frame = settled_menu_frame()
    inject.glide(C.shell, *centre(frame), dur=0.15)               # the file enters the menu
    if not wait(lambda: re.search(r"menu: \d+ file\(s\)", gesture()), 3):
        raise AssertionError("the file never entered the menu")
    time.sleep(0.2)
    if ring_i:                                                    # Shift tap: the other ring
        C.shell.key(SHIFT, False)
        time.sleep(0.1)
        C.shell.key(SHIFT, True)
        if not wait(lambda: f"ring: {names[ring_i]}" in gesture(), 2):
            raise AssertionError(f"the menu did not flip to {names[ring_i]}")
    time.sleep(0.25)                                              # the ring animation
    inject.glide(C.shell, *petal_xy(frame, n, idx), dur=0.3)
    time.sleep(0.35)
    shot(f"{len(RESULTS) + 1:02d}_{safe(name)}_menu")
    C.shell.button(1, False)                                      # the drop
    time.sleep(0.05)
    C.shell.key(SHIFT, False)
    if not wait(lambda: not menus(), 2):
        raise AssertionError("the menu stayed open after the drop")
    want = rf"menu: dropped {len(files)} file\(s\) on {re.escape(label)} \({ring}\)"
    if not wait(lambda: re.search(want, gesture()), 2):
        raise AssertionError(f"no drop on {label} ({ring}) in the app log")
    return gesture()


# ------------------------------------------------------------------ scenario runner
def run(name: str, where: str, files: list[str], ring: str, label: str, verify, tool=None,
        select_extra=None):
    folder = folder_of(where)
    srcs = [str(folder / f) for f in files]
    restore_sources(folder)
    before = snapshot(folder)
    t0 = time.time()
    mark = len(app_text())
    status, msg = "PASS", ""
    try:
        ensure_view(where)
        sources = fingerprint([Path(s) for s in srcs])
        shift_drag(name, where, files, ring, label, bool(select_extra), mark)
        if tool:
            tool()
        outs = new_files(folder, before)
        if not outs:
            raise AssertionError("no output file appeared")
        note = verify(outs, srcs)
        check_untouched(sources)
        msg = note or ", ".join(o.name for o in outs[:3])
    except Exception as e:
        status = "FAIL"
        msg = f"{type(e).__name__}: {e}".strip()
        log(traceback.format_exc())
        log("app output for this scenario:\n" + app_text()[mark:][-3000:])
        shot(f"{len(RESULTS) + 1:02d}_FAIL_{safe(name)}")
        try:
            inject.press(C.shell, "Escape")      # cancel a drag still in flight, close the menu
            time.sleep(0.1)
            C.shell.release_all()
            wait(lambda: not menus(), 3, 0.1)
            close_tool_windows()                 # a tool window the driver left open
        except Exception as e2:
            log("cleanup after the failure failed:", e2)
    dt = time.time() - t0
    RESULTS.append({"name": name, "where": where, "status": status, "detail": msg,
                    "seconds": round(dt, 1), "t": round(time.time() - T0, 1)})
    log(f"{status:4} {where:8} {name:34} {dt:5.1f}s  {msg}")
    stash(folder, before)
    restore_sources(folder)
    time.sleep(0.6)


# ------------------------------------------------------------------ recording
class Recorder(threading.Thread):
    """--record: a screenshot of the session every `period` s (the helper's screenshots
    leave the cursor out, so a dot marks the pointer), turned into e2e_linux.mp4 by ffmpeg
    or, without ffmpeg, into an animated e2e_linux.webp. GNOME's own screencast is not
    reachable from the isolated session (allowlisted callers and PipeWire only)."""

    def __init__(self, period: float = 0.5, scale: float = 0.5):
        super().__init__(daemon=True)
        self.period, self.scale = period, scale
        self.frames = OUT / ".frames"
        self.stop_event = threading.Event()
        self.count = 0

    def run(self):
        from PIL import Image, ImageDraw
        shutil.rmtree(self.frames, ignore_errors=True)
        self.frames.mkdir()
        tmp = C.session.root / "tmp" / "rec.png"
        with ShellHelper(C.session) as helper:
            while not self.stop_event.is_set():
                t = time.monotonic()
                try:
                    helper.screenshot(tmp)
                    x, y, _ = helper.get_pointer()
                    with Image.open(tmp) as im:
                        k = self.scale * im.width / C.session.size[0]
                        im = im.convert("RGB").resize((round(im.width * self.scale),
                                                       round(im.height * self.scale)))
                    d = ImageDraw.Draw(im)
                    px, py = x * k, y * k
                    d.ellipse((px - 6, py - 6, px + 6, py + 6), fill=(255, 255, 255),
                              outline=(0, 0, 0), width=2)
                    self.count += 1
                    im.save(self.frames / f"{self.count:05d}.jpg", quality=80)
                except Exception as e:
                    log("recording frame failed:", e)
                self.stop_event.wait(max(0.0, self.period - (time.monotonic() - t)))

    def finish(self):
        self.stop_event.set()
        self.join(30)
        fps = 1 / self.period
        try:
            if not self.count:
                return
            if shutil.which("ffmpeg"):
                r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", f"{fps:g}",
                                    "-i", str(self.frames / "%05d.jpg"), "-c:v", "libx264",
                                    "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                                    str(OUT / "e2e_linux.mp4")], capture_output=True, text=True)
                if r.returncode == 0:
                    log(f"recording: {self.count} frames → e2e_linux.mp4")
                    return
                log("ffmpeg failed:", r.stderr.strip()[-500:])
            from PIL import Image
            frames = [Image.open(p) for p in sorted(self.frames.glob("*.jpg"))]
            frames[0].save(OUT / "e2e_linux.webp", save_all=True, append_images=frames[1:],
                           duration=int(self.period * 1000), loop=0, quality=60)
            log(f"recording: {self.count} frames → e2e_linux.webp")
        finally:
            shutil.rmtree(self.frames, ignore_errors=True)


# ------------------------------------------------------------------ verifiers (Linux)
def v_same_size(fmt, ext):
    """v_format, and the picture keeps the source's size (a plain conversion)."""
    base = v_format(fmt, ext)

    def v(outs, srcs):
        note = base(outs, srcs)
        assert img_ok(outs[0]) == img_ok(Path(srcs[0])), "size differs from the source"
        return f"{note}, same size as the source"
    return v


# ------------------------------------------------------------------ scenarios
SCENARIOS: list[Scenario] = [
    # ---- conversions from Files
    Scenario("JPG → PNG", "nautilus", ["Photo.jpg"], "Convert", "PNG", v_same_size("PNG", ".png")),
]


DEMO = Path(__file__).resolve().parent


class ScenarioModuleError(RuntimeError):
    """A demo/e2e_linux_*.py module that can't be used: the run stops."""


def collect(demo: Path = DEMO) -> list[tuple[str, list[Scenario]]]:
    """(file name, scenarios) for this file, then every e2e_linux_*.py in `demo` sorted by
    name, imported only now."""
    groups = [("e2e_linux.py", list(SCENARIOS))]
    for path in sorted(demo.glob("e2e_linux_*.py")):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[path.stem] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            sys.modules.pop(path.stem, None)
            raise ScenarioModuleError(f"{path.name} failed to import:\n"
                                      + "".join(traceback.format_exception(e))) from e
        found = getattr(mod, "SCENARIOS", None)
        if not isinstance(found, list) or not all(isinstance(x, Scenario) for x in found):
            raise ScenarioModuleError(f"{path.name} has no SCENARIOS list of Scenario")
        groups.append((path.name, found))
    seen: dict[str, str] = {}
    for file, scenarios in groups:
        for x in scenarios:
            if x.name in seen:
                raise ScenarioModuleError(f"scenario {x.name!r} is in {seen[x.name]} and {file}")
            seen[x.name] = file
    return groups


# ------------------------------------------------------------------ main
def write_results():
    passed = sum(r["status"] == "PASS" for r in RESULTS)
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    lines = [f"# DragShift Linux end-to-end test — {passed}/{len(RESULTS)} passed", "",
             "| # | Where | Scenario | Result | Detail | Time |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(RESULTS, 1):
        mark = "✅" if r["status"] == "PASS" else "❌"
        detail = str(r["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | {r['where']} | {r['name']} | {mark} | {detail} | {r['seconds']}s |")
    (OUT / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"DONE {passed}/{len(RESULTS)} passed")
    return passed


def main(argv=None) -> int:
    global OUT, LOG, T0
    ap = argparse.ArgumentParser(prog="python demo/e2e_linux.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="run only this scenario (repeatable)")
    ap.add_argument("--list", action="store_true", help="print the scenario names and exit")
    ap.add_argument("--out", type=Path, default=OUT, help=f"output folder (default {OUT})")
    ap.add_argument("--record", action="store_true",
                    help="record the session (periodic screenshots → mp4 or webp)")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda signum, _f: sys.exit(128 + signum))   # still clean up
    try:
        groups = collect()
    except ScenarioModuleError as e:
        print(f"scenario modules: {e}", file=sys.stderr)
        return 1
    every = [x for _, scenarios in groups for x in scenarios]
    if a.list:
        for file, scenarios in groups:
            print(file)
            print("".join(f"  {x.name}\n" for x in scenarios), end="")
        return 0
    todo = [s for s in every if not a.only or s.name in a.only]
    unknown = set(a.only) - {s.name for s in every}
    if unknown:
        print(f"unknown scenario(s): {sorted(unknown)}; --list shows them", file=sys.stderr)
        return 2
    OUT = a.out.resolve()
    shutil.rmtree(OUT / "shots", ignore_errors=True)
    (OUT / "shots").mkdir(parents=True, exist_ok=True)
    for f in ("results.json", "results.md", "app_log.txt", "e2e_linux.mp4", "e2e_linux.webp"):
        (OUT / f).unlink(missing_ok=True)
    LOG = open(OUT / "log.txt", "w", encoding="utf-8")
    T0 = time.time()
    RESULTS.clear()
    _WIDGETS.clear()
    missing = missing_tools()
    build = Path(tempfile.mkdtemp(prefix="dragshift-e2e-ext-"))
    rec = None
    session = None
    try:
        if missing:
            raise SessionError("missing programs: " + ", ".join(missing))
        session = C.session = Session(extensions=[build_extension(build)], helper=True)
        session.start()
        log(f"GNOME Shell {session.shell_version} ({session.mode} mode) at "
            f"{session.size[0]}x{session.size[1]}, session root {session.root}")
        C.shell = ShellHelper(session)
        C.shell.wait_ready()
        if not wait(extension_on_bus, 15, 0.2):
            raise RuntimeError(f"DragShift's extension is not on the bus; {session.log_tail()}")
        home = Path(session.env()["HOME"])
        C.exp, C.stash = home / "DragShiftTest", session.root / "outputs"
        C.files = Files(session, C.shell)
        C.desk = home / "Desktop"
        make_files(C.exp)
        if any(s.where == "desktop" for s in todo):
            make_files(C.files.desktop_dir(), "D-")
        C.app_out = OUT / "app_log.txt"
        C.app = start_app()
        log("DragShift is following the extension")
        C.files.open_folder(C.exp)
        if a.record:
            rec = Recorder()
            rec.start()
        shot("00_start")
        T0 = time.time()
        for s in todo:
            run(s.name, s.where, s.files, s.ring, s.label, s.verify, s.tool, s.select_extra)
    except Exception:
        log("E2E ABORTED\n" + traceback.format_exc())
        if session is not None and session.bus_address:
            shot("00_ABORTED")
    finally:
        if session is not None and session.bus_address:
            try:
                C.shell.release_all()
            except Exception:
                pass
        if rec:
            rec.finish()
        stop_app()
        if session is not None and session.bus_address:
            copy_app_log()
            try:
                C.files.close()
            except Exception as e:
                log("closing Files failed:", e)
            try:
                C.shell.close()
            except Exception:
                pass
        if session is not None:
            session.stop()
        shutil.rmtree(build, ignore_errors=True)
        passed = write_results()
        LOG.close()
    return 0 if RESULTS and passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
