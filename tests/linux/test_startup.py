"""Linux start-up (#11) in the isolated GNOME session: the real app (`python -m dragshift`)
with DragShift's extension, at scale 1, 2 (the user's monitor) and fractional 1.5.

- Qt runs as a native Wayland client without QT_QPA_PLATFORM set (app.linux_qpa).
- Its windows carry the app id, so GNOME matches them to io.github.eteen12.DragShift.desktop.
- The log names the platform, the scale and the font Segoe UI resolved to; no font warnings.
- Welcome (first run) and Settings (opened from the tray menu over D-Bus) are drawn at the
  monitor's scale: their text edges stay as steep as at 1x, where a 1x drawing upscaled
  by the compositor (what Xwayland shows, docs/linux/display-backend.md) halves them.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots are written there."""
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift import app
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session, gi_python
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import built_dir, built_zip  # noqa: F401  (fixtures)

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ID = f"{app.LINUX_APP_ID}.desktop"
WELCOME = "Welcome to DragShift — DragShift"
SETTINGS = "Settings — DragShift"
SCALES = {1: (1600, 900), 2: (3200, 1800), 1.5: (2400, 1350)}   # all 1600x900 logical

# Clicks a tray menu entry like the top bar would: StatusNotifierWatcher → our item →
# its com.canonical.dbusmenu → Event(id, "clicked"). Runs with the system python (gi).
TRAY_CLICK = r"""
import json, sys
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
def call(dest, path, iface, method, args):
    return bus.call_sync(dest, path, iface, method, args, None, 0, 3000, None).unpack()
def prop(dest, path, iface, name):
    return call(dest, path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, name)))[0]
items = prop("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
             "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems")
labels = []
def walk(node):
    id_, props, children = node
    yield id_, props.get("label", "").replace("&", "").replace("_", "")
    for c in children:
        yield from walk(c)
for item in items:
    name, _, path = item.partition("@")
    menu = prop(name, path or "/StatusNotifierItem", "org.kde.StatusNotifierItem", "Menu")
    _, layout = call(name, menu, "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
    for id_, label in walk(layout):
        labels.append(label)
        if label == sys.argv[1]:
            call(name, menu, "com.canonical.dbusmenu", "Event",
                 GLib.Variant("(isvu)", (id_, "clicked", GLib.Variant("i", 0), 0)))
            print(json.dumps({"clicked": id_, "items": items}))
            sys.exit(0)
print(json.dumps({"items": items, "labels": labels}))
sys.exit(1)
"""


def _evidence(name: str, img: Image.Image) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        img.save(Path(out) / name)


# The sharpest steps between neighbouring device pixels (white text on the dark glass).
# Drawn at the monitor's scale they stay steep: 98-117 at scale 2 and 1.5 here. The same
# windows drawn at 1x and upscaled by the compositor (Xwayland's case) reach only 44-65.
SHARP_EDGE = 80


def _edge(img: Image.Image) -> float:
    """99.5th percentile of |difference| between horizontally and vertically adjacent
    pixels of the grey image, in 8-bit levels."""
    a = np.asarray(img.convert("L"), dtype=np.float32)
    steps = np.concatenate([np.abs(np.diff(a, axis=1)).ravel(), np.abs(np.diff(a, axis=0)).ravel()])
    return float(np.percentile(steps, 99.5))


def _check_sharp(crop: Image.Image, scale: float, name: str) -> None:
    w, h = crop.size
    upscaled = crop.resize((round(w / scale), round(h / scale)), Image.BOX).resize((w, h), Image.BILINEAR)
    edge, ref = _edge(crop), _edge(upscaled)
    print(f"\n{name} at scale {scale:g}: sharpest edges {edge:.0f} (drawn at 1x and upscaled: {ref:.0f})")
    assert edge >= SHARP_EDGE and edge > 1.3 * ref


@pytest.fixture(scope="module", params=list(SCALES), ids=lambda s: f"scale{s:g}")
def started(request, built_dir):  # noqa: F811
    """A session at the given scale with the extension, a desktop file for the app id and
    DragShift started the way a user starts it (no QT_QPA_PLATFORM, first run)."""
    require_session_tools()
    scale = request.param
    desktop = ("[Desktop Entry]\nType=Application\nName=DragShift\n"
               f"Exec={sys.executable} -m dragshift %F\n"
               f"Icon={ROOT / 'dragshift' / 'assets' / 'dragshift.png'}\nTerminal=false\n")
    with Session(size=SCALES[scale], extensions=[built_dir], helper=True, scale=scale,
                 data_files={f"applications/{DESKTOP_ID}": desktop}) as s, \
            ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(5.0)   # the shell's boot banner ("Screen Lock disabled") goes away
        env = s.env()
        assert "QT_QPA_PLATFORM" not in env
        out = s.root / "tmp" / "dragshift.out"
        with open(out, "w") as f:
            proc = s.popen([sys.executable, "-m", "dragshift"], cwd=ROOT,
                           env={"PYTHONPATH": str(ROOT)},
                           stdout=f, stderr=subprocess.STDOUT)
        try:
            welcome = shell.wait_for_window(title=WELCOME, timeout=20)
            time.sleep(1.0)   # first frames and the glass backdrop
            yield {"session": s, "shell": shell, "scale": scale, "proc": proc, "out": out,
                   "welcome": welcome,
                   "log": Path(env["XDG_STATE_HOME"]) / "DragShift" / "log.txt"}
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _window_crop(started, window: dict, name: str) -> Image.Image:
    s, scale = started["session"], started["scale"]
    shot = Image.open(started["shell"].screenshot(s.root / "tmp" / f"{name}-full.png"))
    x, y, w, h = window["frame"]
    crop = shot.crop(tuple(round(v * scale) for v in (x, y, x + w, y + h)))
    tag = f"scale{scale:g}"
    _evidence(f"{name}-{tag}.png", crop)
    _evidence(f"desktop-{name}-{tag}.png", shot)
    return crop


def test_native_wayland_with_the_app_id(started):
    w = started["welcome"]
    assert w["client"] == "wayland"
    assert w["wm_class"] == app.LINUX_APP_ID
    assert w["app"] == DESKTOP_ID        # dock icon and name come from the desktop file


def test_log_names_platform_scale_and_font(started):
    log = started["log"].read_text(encoding="utf-8")
    run = log.rsplit("==== DragShift", 1)[1]
    assert "\nplatform: wayland\n" in run
    assert "screen Meta-0: 1600x900 at (0, 0), output scale " in run
    font = run.split("font: Segoe UI → ", 1)[1].splitlines()[0]
    assert font in app.LINUX_FONTS
    out = started["out"].read_text(errors="replace")
    assert "Segoe" not in out and "font family" not in out.lower(), out


def test_welcome_drawn_at_the_monitor_scale(started):
    crop = _window_crop(started, started["welcome"], "welcome")
    assert crop.size == tuple(round(v * started["scale"]) for v in started["welcome"]["frame"][2:])
    if started["scale"] != 1:
        _check_sharp(crop, started["scale"], "welcome")


def test_settings_from_the_tray_drawn_at_the_monitor_scale(started):
    s, shell = started["session"], started["shell"]
    deadline = time.monotonic() + 15   # the tray item registers shortly after start-up
    while True:
        r = s.run([gi_python(), "-I", "-c", TRAY_CLICK, "Settings…"])
        if r.returncode == 0 or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    assert r.returncode == 0, r.stdout + r.stderr
    win = shell.wait_for_window(title=SETTINGS, timeout=15)
    assert win["wm_class"] == app.LINUX_APP_ID and win["app"] == DESKTOP_ID
    time.sleep(1.0)
    crop = _window_crop(started, win, "settings")
    if started["scale"] != 1:
        _check_sharp(crop, started["scale"], "settings")
    assert started["proc"].poll() is None
