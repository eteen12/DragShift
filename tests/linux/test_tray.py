"""Tray icon through StatusNotifierItem (#46) in the isolated GNOME session, with the real
app (`python -m dragshift`) and Ubuntu's AppIndicator extension as the top bar's host.

- The item is registered with org.kde.StatusNotifierWatcher and sends pixmap frames only
  (no icon name, so nothing depends on an installed icon theme).
- The top bar draws the 32 px frame 1:1 at scale 2 and the 16 px frame at scale 1, on
  Yaru's top bar in the light and the dark colour scheme.
- A click on the icon opens the shell menu; a double click (Activate → Trigger) opens the
  file picker. The six menu entries work over com.canonical.dbusmenu.
- A second instance hands over and adds no item; Quit unregisters it.
- Without a StatusNotifier host (AppIndicator disabled) the app keeps running, logs why,
  and opening DragShift again opens Settings. Enabling the host later brings the item in.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots are written there."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift import app
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session, gi_python
from tests.linux.shell import ShellHelper
from tests.linux.test_startup import TRAY_CLICK

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
APPINDICATOR = "ubuntu-appindicators@ubuntu.com"
MENU = ["Enabled", "Open a file…", "Settings…", "How to use", "Tip the developer ♥",
        f"Quit {app.APP_NAME}"]
PICKER = "Choose file(s) for DragShift"
SETTINGS = "Settings — DragShift"
WELCOME = "Welcome to DragShift — DragShift"
PANEL = 32          # GNOME 46 top bar height, logical px (the icon is 16 px in it)

# Watcher → our item (Id "DragShift"): its properties, pixmap frames (base64 ARGB32, network
# byte order) and menu layout; `activate` also calls Activate like a double click in the
# top bar. Runs with the system python (gi) inside the session.
SNI = r"""
import base64, json, sys
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
def call(dest, path, iface, method, args):
    return bus.call_sync(dest, path, iface, method, args, None, 0, 3000, None).unpack()
def props(dest, path, iface):
    return call(dest, path, "org.freedesktop.DBus.Properties", "GetAll",
                GLib.Variant("(s)", (iface,)))[0]
try:
    w = props("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
              "org.kde.StatusNotifierWatcher")
except GLib.Error as e:
    print(json.dumps({"watcher": False, "items": [], "ours": [], "error": e.message}))
    sys.exit(0)
out = {"watcher": True, "host": w["IsStatusNotifierHostRegistered"],
       "items": w["RegisteredStatusNotifierItems"], "ours": []}
def walk(node):
    id_, p, children = node
    if p.get("type") != "separator":
        yield {"id": id_, "label": p.get("label", "").replace("_", ""),
               **{k: p[k] for k in ("toggle-type", "toggle-state", "enabled") if k in p}}
    for c in children:
        yield from walk(c)
for item in out["items"]:
    name, _, path = item.partition("@")
    path = path or "/StatusNotifierItem"
    try:
        p = props(name, path, "org.kde.StatusNotifierItem")
    except GLib.Error:   # gone (e.g. just quit) but not yet dropped by the watcher
        continue
    if p.get("Id") != "DragShift":
        continue
    frames = {f"{fw}x{fh}": base64.b64encode(bytes(d)).decode() for fw, fh, d in p["IconPixmap"]}
    p = {k: v for k, v in p.items() if "Pixmap" not in k and k != "ToolTip"}
    _, layout = call(name, p["Menu"], "com.canonical.dbusmenu", "GetLayout",
                     GLib.Variant("(iias)", (0, -1, [])))
    if sys.argv[1:] == ["activate"]:
        # Qt answers only after the slot returns, i.e. once the file picker is closed
        # (the shell calls it asynchronously), so don't wait for the reply.
        bus.call(name, path, "org.kde.StatusNotifierItem", "Activate",
                 GLib.Variant("(ii)", (0, 0)), None, 0, -1, None, None)
        bus.flush_sync(None)
    out["ours"].append({"item": item, "props": p, "frames": frames,
                        "menu": list(walk(layout))[1:]})   # [0] is the menu's root
print(json.dumps(out))
"""


def _evidence(name: str, img: Image.Image) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if out:
        img.save(Path(out) / name)


def sni(s: Session, *args: str) -> dict:
    r = s.run([gi_python(), "-I", "-c", SNI, *args])
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def wait_sni(s: Session, ok, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        state = sni(s)
        if ok(state) or time.monotonic() > deadline:
            return state
        time.sleep(0.3)


def click_entry(s: Session, label: str) -> None:
    r = s.run([gi_python(), "-I", "-c", TRAY_CLICK, label])
    assert r.returncode == 0, r.stdout + r.stderr


def run_log(started) -> str:
    """This run's part of DragShift's log."""
    return started["log"].read_text(encoding="utf-8").rsplit("==== DragShift", 1)[1]


def wait_log(started, text: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while text not in (log := run_log(started)) and time.monotonic() < deadline:
        time.sleep(0.2)
    return log


def frame(state: dict, size: int) -> Image.Image:
    """Our item's size×size pixmap frame as RGBA."""
    import base64
    raw = base64.b64decode(state["ours"][0]["frames"][f"{size}x{size}"])
    argb = np.frombuffer(raw, np.uint8).reshape(size, size, 4)
    return Image.fromarray(np.ascontiguousarray(argb[..., [1, 2, 3, 0]]), "RGBA")


def screenshot(started, name: str) -> Image.Image:
    shell, s = started["shell"], started["session"]
    return Image.open(shell.screenshot(s.root / "tmp" / f"{name}.png")).convert("RGB")


def find_icon(shot: Image.Image, scale: float) -> tuple[int, int, int, int]:
    """Device-pixel bbox of the colour icon in the right half of the top bar: the densest
    icon-sized run of colourful columns (the other status icons are grey; the clock's
    subpixel-antialiased text only has thin coloured fringes)."""
    x0, size = shot.width // 2, round(16 * scale)
    a = np.asarray(shot.crop((x0, 0, shot.width, round(PANEL * scale)))).astype(int)
    colourful = (a.max(2) - a.min(2)) > 60
    density = np.convolve(colourful.sum(0), np.ones(size), "valid")
    left = int(density.argmax())
    assert density[left] > size * size / 4, "no colour icon in the top bar"
    ys, xs = np.nonzero(colourful[:, left:left + size])
    return x0 + left + xs.min(), ys.min(), x0 + left + xs.max() + 1, ys.max() + 1


def match_error(shot: Image.Image, box, sprite: Image.Image) -> float:
    """Mean |difference| (8-bit levels) between the screen and `sprite` composited on the
    top bar's colour, at the best offset around `box` (the sprite's border is transparent)."""
    w, h = sprite.size
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    panel = shot.getpixel((round(box[0] - w), round(cy)))
    flat = Image.new("RGBA", sprite.size, panel + (255,))
    want = np.asarray(Image.alpha_composite(flat, sprite).convert("RGB")).astype(float)
    best = 255.0
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            x, y = round(cx - w / 2) + dx, round(cy - h / 2) + dy
            got = np.asarray(shot.crop((x, y, x + w, y + h))).astype(float)
            best = min(best, float(np.abs(got - want).mean()))
    return best


def luminance(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _start(s: Session, quiet: bool, path_dir: Path | None = None) -> subprocess.Popen:
    env = {"PYTHONPATH": str(ROOT)}
    if path_dir:
        env["PATH"] = f"{path_dir}:{s.env()['PATH']}"
    out = open(s.root / "tmp" / "dragshift.out", "a")
    return s.popen([sys.executable, "-m", "dragshift", *(["--quiet"] if quiet else [])],
                   cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _welcomed(s: Session) -> None:
    """Not a first run, so no Welcome window covers the top bar."""
    conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")


def _color_scheme(s: Session, scheme: str) -> None:
    r = s.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", scheme])
    assert r.returncode == 0, r.stderr


def _close(shell, window: dict) -> None:
    """Focus the window and press Escape until it is gone."""
    deadline = time.monotonic() + 10
    while any(w["id"] == window["id"] for w in shell.list_windows()):
        assert time.monotonic() < deadline, f"{window['title']} did not close"
        shell.activate_window(window["id"])
        time.sleep(0.3)
        inject.press(shell, "Escape")
        time.sleep(1.0)


# ---------------------------------------------------------------- with AppIndicator
@pytest.fixture(scope="class")
def started():
    """A session at the user's 200 % with DragShift running (--quiet, not a first run) and a
    fake xdg-open that records what it was asked to open."""
    require_session_tools()
    with Session(size=(3200, 1800), helper=True, scale=2) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(5.0)   # the shell's boot banner ("Screen Lock disabled") goes away
        _welcomed(s)
        fake = s.root / "tmp" / "bin"
        fake.mkdir()
        opened = s.root / "tmp" / "xdg-open.log"
        (fake / "xdg-open").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{opened}"\n')
        (fake / "xdg-open").chmod(0o755)
        proc = _start(s, quiet=True, path_dir=fake)
        try:
            state = wait_sni(s, lambda st: st["ours"])
            yield {"session": s, "shell": shell, "proc": proc, "state": state,
                   "opened": opened, "fake": fake,
                   "log": Path(s.env()["XDG_STATE_HOME"]) / "DragShift" / "log.txt"}
        finally:
            _stop(proc)
            _color_scheme(s, "default")


class TestWithAppIndicator:
    """Scale 2, Ubuntu's AppIndicator extension as the host."""

    def test_item_registered_with_pixmap_frames(self, started):
        state = started["state"]
        assert state["host"] is True
        assert len(state["ours"]) == 1, state["items"]
        ours = state["ours"][0]
        p = ours["props"]
        assert p["Status"] == "Active" and p["Category"] == "ApplicationStatus"
        assert p["Title"] == app.APP_NAME
        assert p["IconName"] == ""        # pixmaps only: no icon theme lookup can fail
        # exact frames for the top bar's 16 px at scale 1, 2 and 3
        assert {"16x16", "32x32", "48x48"} <= set(ours["frames"]), list(ours["frames"])
        assert "no StatusNotifierWatcher" not in run_log(started)


    def test_menu_lists_the_six_entries(self, started):
        menu = started["state"]["ours"][0]["menu"]
        assert [m["label"] for m in menu] == MENU
        enabled = menu[0]
        assert enabled["toggle-type"] == "checkmark" and enabled["toggle-state"] == 1


    @pytest.mark.parametrize("scheme", ["default", "prefer-dark"], ids=["light", "dark"])
    def test_icon_crisp_on_the_top_bar(self, started, scheme):
        """At scale 2 the top bar shows the 32 px frame pixel for pixel; a 16 px frame
        upscaled (what a blurry icon would look like) is far off. Yaru's top bar is dark in
        both colour schemes, and the colour icon stands out on it."""
        s, shell = started["session"], started["shell"]
        _color_scheme(s, scheme)
        inject.glide(shell, 800, 600)   # no hover highlight behind the icon
        time.sleep(1.5)
        shot = screenshot(started, f"topbar-{scheme}")
        box = find_icon(shot, 2)
        sharp = frame(started["state"], 32)
        blurry = frame(started["state"], 16).resize((32, 32), Image.BILINEAR)
        err, err_blurry = match_error(shot, box, sharp), match_error(shot, box, blurry)
        print(f"\n{scheme}: icon at {box}, 32 px frame off by {err:.1f}, "
              f"16 px upscaled off by {err_blurry:.1f}")
        assert box[2] - box[0] <= 32 and box[3] - box[1] <= 32
        assert err < 3 and err_blurry > 3 * err + 3
        panel = shot.getpixel((box[0] - 32, (box[1] + box[3]) // 2))
        icon = np.asarray(shot.crop(box)).reshape(-1, 3)
        assert luminance(panel) < 60, panel
        assert np.mean([luminance(c) for c in icon]) - luminance(panel) > 60
        tag = "light" if scheme == "default" else "dark"
        _evidence(f"topbar-{tag}-scale2.png", shot.crop((shot.width - 900, 0, shot.width, 80)))
        _evidence(f"icon-{tag}-scale2-x8.png",
                  shot.crop((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4)).resize(
                      ((box[2] - box[0] + 8) * 8, (box[3] - box[1] + 8) * 8), Image.NEAREST))
        _evidence(f"desktop-{tag}-scale2.png", shot)


    def test_icon_crisp_at_scale_1(self, started):
        s, shell = started["session"], started["shell"]
        s.set_scale(1)
        try:
            time.sleep(2.0)
            inject.glide(shell, 800, 600)
            shot = screenshot(started, "topbar-scale1")
            box = find_icon(shot, 1)
            err = match_error(shot, box, frame(started["state"], 16))
            print(f"\nscale 1: icon at {box}, 16 px frame off by {err:.1f}")
            assert box[2] - box[0] <= 16 and err < 3
            _evidence("topbar-light-scale1.png", shot.crop((shot.width - 450, 0, shot.width, 40)))
        finally:
            s.set_scale(2)
            time.sleep(2.0)


    def test_click_opens_the_menu_and_double_click_the_file_picker(self, started):
        s, shell = started["session"], started["shell"]
        _color_scheme(s, "prefer-dark")   # the user's scheme
        inject.glide(shell, 800, 600)
        time.sleep(1.0)
        before = screenshot(started, "before-click")
        box = find_icon(before, 2)
        cx, cy = (box[0] + box[2]) / 4, (box[1] + box[3]) / 4   # logical
        inject.click(shell, cx, cy)
        time.sleep(1.5)   # AppIndicator waits out the double-click time, then opens the menu
        try:
            after = screenshot(started, "menu")
            _evidence("menu-dark-scale2.png", after.crop((after.width - 900, 0, after.width, 800)))
            # the menu's text and frame under the icon (dark on the dark wallpaper, so count
            # pixels that changed rather than the mean)
            below = (box[0] - 400, box[3] + 20, box[2] + 100, box[3] + 500)
            diff = np.abs(np.asarray(after.crop(below)).astype(int)
                          - np.asarray(before.crop(below)).astype(int)).max(2)
            changed = float((diff > 40).mean())
            assert changed > 0.02, f"no menu under the icon ({changed:.1%} of pixels changed)"
            assert not [w for w in shell.list_windows() if w["title"] == PICKER]
        finally:
            inject.press(shell, "Escape")   # closes the shell menu
            time.sleep(0.5)

        inject.click(shell, cx, cy, count=2)
        picker = shell.wait_for_window(title=PICKER, timeout=15)
        time.sleep(1.0)
        _evidence("file-picker-from-double-click.png", screenshot(started, "picker"))
        _close(shell, picker)


    def test_activate_opens_the_file_picker(self, started):
        s, shell = started["session"], started["shell"]
        sni(s, "activate")
        picker = shell.wait_for_window(title=PICKER, timeout=15)
        _close(shell, picker)


    def test_enabled_toggles_the_trigger(self, started):
        s = started["session"]
        click_entry(s, "Enabled")
        assert "tray: trigger disabled" in wait_log(started, "tray: trigger disabled")
        menu = wait_sni(s, lambda st: st["ours"][0]["menu"][0]["toggle-state"] == 0)["ours"][0]["menu"]
        assert menu[0]["toggle-state"] == 0
        click_entry(s, "Enabled")
        assert "tray: trigger enabled" in wait_log(started, "tray: trigger enabled")
        menu = wait_sni(s, lambda st: st["ours"][0]["menu"][0]["toggle-state"] == 1)["ours"][0]["menu"]
        assert menu[0]["toggle-state"] == 1


    def test_second_instance_adds_no_item(self, started):
        s, shell = started["session"], started["shell"]
        r = s.run([sys.executable, "-m", "dragshift"], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                  timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        time.sleep(1.5)
        state = sni(s)
        assert [o["item"] for o in state["ours"]] == [started["state"]["ours"][0]["item"]]
        # the tray is there, so this is the "already running" toast, not Settings
        assert not [w for w in shell.list_windows() if w["title"] == SETTINGS]


    @pytest.mark.parametrize("label, title", [("Settings…", SETTINGS), ("How to use", WELCOME)])
    def test_menu_opens_window(self, started, label, title):
        s, shell = started["session"], started["shell"]
        click_entry(s, label)
        w = shell.wait_for_window(title=title, timeout=15)
        assert w["wm_class"] == app.LINUX_APP_ID
        if label == "Settings…":
            time.sleep(1.0)
            _evidence("settings-from-tray.png", screenshot(started, "settings"))
        _close(shell, w)


    def test_tip_opens_the_url_with_xdg_open(self, started):
        click_entry(started["session"], "Tip the developer ♥")
        deadline = time.monotonic() + 10
        while not started["opened"].exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert started["opened"].read_text().split() == [app.TIP_URL]


    def test_quit_unregisters_the_item(self, started):
        s = started["session"]
        click_entry(s, f"Quit {app.APP_NAME}")
        assert started["proc"].wait(15) == 0
        state = wait_sni(s, lambda st: not st["ours"])
        assert state["ours"] == [] and state["watcher"]


# ---------------------------------------------------------------- without a host
@pytest.fixture(scope="class")
def no_host():
    """A session with the AppIndicator extension disabled (no StatusNotifierWatcher), then
    DragShift started as a user would (not --quiet, not a first run)."""
    require_session_tools()
    with Session(helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(5.0)
        r = s.run(["gnome-extensions", "disable", APPINDICATOR])
        assert r.returncode == 0, r.stderr
        assert not wait_sni(s, lambda st: not st["watcher"])["watcher"]
        _welcomed(s)
        proc = _start(s, quiet=False)
        try:
            log = Path(s.env()["XDG_STATE_HOME"]) / "DragShift" / "log.txt"
            started = {"session": s, "shell": shell, "proc": proc, "log": log}
            deadline = time.monotonic() + 20
            while not (log.exists() and "==== DragShift" in log.read_text()
                       and "font:" in run_log(started)):
                assert time.monotonic() < deadline and proc.poll() is None
                time.sleep(0.2)
            yield started
        finally:
            _stop(proc)
            s.run(["gnome-extensions", "enable", APPINDICATOR])


class TestWithoutHost:
    """AppIndicator disabled before DragShift starts, enabled later."""

    def test_no_host_logs_and_keeps_running(self, no_host):
        log = wait_log(no_host, "tray: no StatusNotifierWatcher")
        assert ("tray: no StatusNotifierWatcher; use Files → Scripts → DragShift or "
                "`dragshift <file>`") in log
        time.sleep(1.0)
        assert no_host["proc"].poll() is None
        _evidence("no-host-desktop.png", screenshot(no_host, "no-host"))


    def test_no_host_relaunch_opens_settings(self, no_host):
        s, shell = no_host["session"], no_host["shell"]
        r = s.run([sys.executable, "-m", "dragshift"], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                  timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        w = shell.wait_for_window(title=SETTINGS, timeout=15)
        assert w["wm_class"] == app.LINUX_APP_ID
        time.sleep(1.0)
        _evidence("no-host-relaunch-settings.png", screenshot(no_host, "no-host-settings"))
        _close(shell, w)


    def test_host_enabled_later_gets_the_item(self, no_host):
        s = no_host["session"]
        r = s.run(["gnome-extensions", "enable", APPINDICATOR])
        assert r.returncode == 0, r.stderr
        state = wait_sni(s, lambda st: st["ours"])
        assert len(state["ours"]) == 1
        assert [m["label"] for m in state["ours"][0]["menu"]] == MENU
        click_entry(s, f"Quit {app.APP_NAME}")
        assert no_host["proc"].wait(15) == 0
