"""Files' right-click → Scripts → DragShift… (#51) in the isolated GNOME session, with the
real app.

The setup is tests/linux/test_handoff.py's (#50): the launcher in the session's
HOME/.local/bin, XDG_DATA_HOME/dragshift/{venv,src} pointing at this venv and checkout.
On top of it, packaging/linux/nautilus-scripts/DragShift… goes into the session's
XDG_DATA_HOME/nautilus/scripts, where Nautilus looks for scripts (never the real one).

With the pointer, as a user does it: right-click a file → the context menu's Scripts
submenu → DragShift…. DragShift's menu then opens at the pointer with that file:
- one file, DragShift running;
- three files at once, one named with a space and unicode and one with a newline:
  one menu for all three (a file whose name came through wrong would be dropped);
- one file, DragShift not running: the script starts it.
Nautilus 46 has no Scripts submenu on a folder's background (only with a selection), so
the script's no-selection case is covered by tests/test_nautilus_script.py only.
The time from the click on DragShift… to the menu at the pointer is printed. With
DRAGSHIFT_EVIDENCE_DIR set, screenshots are written there.

GTK 4's menu items have no accessible names (#50), so the items are found by their
role and state: Scripts is the item that opens a submenu, and in that submenu the
scripts come between its header and "Open Scripts Folder". The session has only this
script, and the menu opening for the right files proves the right item was chosen."""
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift.radial import MENU_TITLE
from tests.linux import inject
from tests.linux.files import Files
from tests.linux.session import gi_python
from tests.linux.test_extension import built_dir, built_zip  # noqa: F401
from tests.linux.test_handoff import (App, close_menu, dragshift_processes,  # noqa: F401
                                      env, log_since, log_size, shot)
from tests.linux.test_menu_place import expected_tl

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "linux" / "nautilus-scripts" / "DragShift…"
ODD = ["two words é 写真.jpg", "new\nline.jpg"]

# Runs with the system python (gi): the menu items of Nautilus' open popover menu, with
# their extents relative to the popover's root, which is the popover's surface plus its
# shadow margins.
MENU_ITEMS = r"""
import json
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
out = {"items": []}
def ext(node):
    e = node.get_extents(Atspi.CoordType.WINDOW)
    return [e.x, e.y, e.width, e.height]
def walk(node, in_menu):
    role = node.get_role_name()
    if role == "menu" and not in_menu:
        out["menu"] = ext(node)
        in_menu = True
    if in_menu and role == "menu item":
        states = node.get_state_set()
        out["items"].append({"ext": ext(node),
                             "submenu": states.contains(Atspi.StateType.EXPANDABLE)})
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            walk(child, in_menu)
desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    a = desktop.get_child_at_index(i)
    if a is not None and a.get_name() == "org.gnome.Nautilus":
        walk(a, False)
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def scripted(env):
    scripts = env.data / "nautilus" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    assert os.access(scripts / SCRIPT.name, os.X_OK)
    env.session.run(["nautilus", "-q"], timeout=30)     # a fresh Files reads the folder
    return env


@pytest.fixture
def folder(scripted):
    d = scripted.home / f"pics-{time.monotonic_ns()}"
    d.mkdir()
    colors = [(200, 40, 40), (40, 160, 40), (40, 40, 200)]
    for name, color in zip(["Photo.jpg", *ODD], colors):
        Image.new("RGB", (64, 48), color).save(d / name, "JPEG", quality=95)
    return d


def popover(env, timeout: float = 5.0) -> tuple[dict, list[dict]]:
    """The open menu's window and its items, centre points in stage coordinates."""
    deadline = time.monotonic() + timeout
    while True:
        wins = [w for w in env.shell.list_windows() if w["window_type"] == "dropdown_menu"
                and w["app"] == "org.gnome.Nautilus.desktop" and w["frame"][2] > 0]
        if wins:
            r = env.inside.run([gi_python(), "-I", "-c", MENU_ITEMS])
            assert r.returncode == 0, r.stdout + r.stderr
            tree = json.loads(r.stdout)
            if tree.get("menu") and tree["items"]:
                break
        assert time.monotonic() < deadline, "no Files context menu"
        time.sleep(0.1)
    x, y, w, h = wins[-1]["frame"]
    mx, my, mw, mh = tree["menu"]
    top = y - (mh - h) // 2                     # the root includes the shadow margins
    for item in tree["items"]:
        ix, iy, iw, ih = item["ext"]
        item["center"] = (x + w // 2, top + iy - my + ih // 2)
    return wins[-1], tree["items"]


def choose_dragshift(env, at: tuple[int, int], tag: str) -> float:
    """Right-click at `at` → Scripts → DragShift…, with the pointer. Returns the time of
    the click on DragShift…"""
    inject.click(env.shell, *at, button="right")
    time.sleep(0.8)
    _, items = popover(env)
    shot(env, f"script-{tag}-1-context-menu")
    scripts = [i for i in items if i["submenu"]]
    assert len(scripts) == 1, items         # Scripts is the only submenu
    inject.click(env.shell, *scripts[0]["center"], dur=0.3)
    time.sleep(0.8)                         # the submenu slides in
    _, items = popover(env)
    # header (back to the menu), the scripts, "Open Scripts Folder"
    assert items[0]["submenu"] and len(items) == 3, items
    target = items[1]["center"]
    inject.glide(env.shell, *target, 0.3)
    time.sleep(0.3)
    shot(env, f"script-{tag}-2-scripts-submenu")
    t0 = time.monotonic()
    inject.click(env.shell)
    return t0


def wait_menu_at_pointer(env, t0: float, timeout: float = 15.0) -> dict:
    """The menu once the shell has put it centred on the pointer, where the click on
    DragShift… was (#34 keeps it inside the work area). `seen` is when it got there:
    Mutter maps it at its own spot first, and the extension moves it before it draws."""
    px, py, _ = env.shell.get_pointer()
    tl = expected_tl(px, py)
    deadline = t0 + timeout
    while True:
        w = next((w for w in env.shell.list_windows() if w["title"] == MENU_TITLE), None)
        if w and max(abs(w["frame"][0] - tl[0]), abs(w["frame"][1] - tl[1])) <= 2:
            w["seen"], w["pointer"] = time.monotonic(), (px, py)
            return w
        assert time.monotonic() < deadline, f"no menu at {tl} (pointer {px},{py}): {w}"
        time.sleep(0.01)


def check_menu(env, t0: float, n: int, log_from: int, tag: str) -> dict:
    win = wait_menu_at_pointer(env, t0, timeout=30)
    x, y, w, h = win["frame"]
    print(f"\n{tag}: menu at the pointer {(win['seen'] - t0) * 1000:.0f} ms after the "
          f"click on DragShift… (frame {x},{y} {w}x{h}, pointer {win['pointer']})")
    assert list(dragshift_processes(env)) == [win["pid"]]           # one app
    time.sleep(0.6)
    shot(env, f"script-{tag}-3-menu")
    log = log_since(env, log_from)
    assert log.count("click menu: ") == 1 and f"click menu: {n} file(s)" in log, log
    return win


def _cleanup(env, files, app) -> None:
    files.close()
    if app:
        app.stop()
    for pid in dragshift_processes(env):
        os.kill(pid, 15)
    deadline = time.monotonic() + 10
    while dragshift_processes(env) and time.monotonic() < deadline:
        time.sleep(0.1)
    env.shell.release_all()


@pytest.mark.parametrize("running", [True, False], ids=["running", "cold"])
def test_one_file(scripted, folder, running):
    env = scripted
    app = App(env, "--quiet") if running else None
    if not running:
        assert not dragshift_processes(env)
    files = Files(env.inside, env.shell)
    tag = "one-file" + ("" if running else "-cold")
    try:
        files.open_folder(folder, view="icon")
        photo = files.icon_center("Photo.jpg")
        inject.click(env.shell, *photo)
        time.sleep(0.3)
        log_from = log_size(env)
        t0 = choose_dragshift(env, photo, tag)
        win = check_menu(env, t0, 1, log_from, tag)
        if running:
            assert win["pid"] == app.proc.pid
            assert "IPC: 1 file(s) handed over" in app.text()
        else:       # the script ran the launcher, which started DragShift with the file
            argv = Path(f"/proc/{win['pid']}/cmdline").read_bytes().split(b"\0")[:-1]
            assert argv[0].decode() == str(env.data / "dragshift" / "venv" / "bin" / "python")
            assert argv[3:] == [os.fsencode(folder / "Photo.jpg")]
        close_menu(env)
    finally:
        _cleanup(env, files, app)


def test_three_files_with_odd_names_open_one_menu(scripted, folder):
    env = scripted
    app = App(env, "--quiet")
    files = Files(env.inside, env.shell)
    try:
        files.open_folder(folder, view="icon")
        photo = files.icon_center("Photo.jpg")
        inject.click(env.shell, *photo)
        time.sleep(0.3)
        with inject.hold_key(env.shell, "Control_L"):
            inject.press(env.shell, "a")                # select all three
        time.sleep(0.5)
        log_from = log_size(env)
        t0 = choose_dragshift(env, photo, "three-files")
        # a name split or mangled on the way would not exist, and the app drops it
        win = check_menu(env, t0, 3, log_from, "three-files")
        assert win["pid"] == app.proc.pid
        assert "IPC: 3 file(s) handed over" in app.text()
        assert app.text().count("menu: at") == 1
        close_menu(env)
    finally:
        _cleanup(env, files, app)
