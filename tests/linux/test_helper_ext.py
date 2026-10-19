"""The test-helper extension and its client (tests/linux/ext, tests/linux/shell.py)."""
import secrets
import shutil
import subprocess
import time

import pytest
from PIL import Image

from tests.linux import session as session_mod
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session, gi_python
from tests.linux.shell import EXTENSION_DIR, UUID, HelperError, ShellHelper

gnome = pytest.mark.gnome

# A GTK 3 window on Xwayland that prints its own X11 window id (system python with gi).
_X11_WINDOW = """
import sys
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gtk, GdkX11
w = Gtk.Window(title=sys.argv[1])
w.set_default_size(320, 200)
w.connect("destroy", Gtk.main_quit)
w.show_all()
print(w.get_window().get_xid(), flush=True)
Gtk.main()
"""


def _until(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not (value := fn()) and time.monotonic() < deadline:
        time.sleep(0.1)
    return value


def _focused(helper, window_id):
    return any(w["id"] == window_id and w["focused"] for w in helper.list_windows())


def _x11_window(session, title):
    """Start the GTK 3 X11 window; returns (process, its X11 window id)."""
    probe = subprocess.run([gi_python(), "-I", "-c",
                            "import gi; gi.require_version('GdkX11', '3.0')"],
                           capture_output=True)
    if probe.returncode != 0:
        pytest.skip("GTK 3 introspection (gir1.2-gtk-3.0) not installed")
    p = session.popen([gi_python(), "-I", "-c", _X11_WINDOW, title],
                      env={"GDK_BACKEND": "x11"}, stdout=subprocess.PIPE, text=True)
    return p, int(p.stdout.readline())


@gnome
def test_version(shell_helper):
    assert shell_helper.version() == "1"


@gnome
def test_screenshot_is_a_full_stage_png(gnome_session, shell_helper):
    path = shell_helper.screenshot()
    assert path.parent == gnome_session.root / "tmp"
    with Image.open(path) as im:
        assert im.format == "PNG"
        assert im.size == (1600, 900)
        lo, hi = im.convert("L").getextrema()
    assert hi - lo > 50, "screenshot is a uniform image"


@gnome
def test_screenshot_area(gnome_session, shell_helper):
    path = shell_helper.screenshot_area(100, 50, 320, 200,
                                        gnome_session.root / "home" / "area.png")
    with Image.open(path) as im:
        assert (im.format, im.size) == ("PNG", (320, 200))


@gnome
def test_screenshot_only_writes_inside_the_session_root(gnome_session, shell_helper, tmp_path):
    outside = tmp_path / "outside.png"           # pytest's tmp dir: not the session's
    escape = gnome_session.root / "tmp" / ".." / ".." / f"escape-{secrets.token_hex(4)}.png"
    for path in (outside, escape, "relative.png"):
        with pytest.raises(HelperError, match="InvalidArgs"):
            shell_helper.screenshot(path)
    with pytest.raises(HelperError, match="empty area"):
        shell_helper.screenshot_area(0, 0, 0, 10)
    assert not outside.exists()
    assert not (gnome_session.root.parent / escape.name).exists()


@gnome
def test_get_pointer(shell_helper):
    x, y, mask = shell_helper.get_pointer()
    assert 0 <= x < 1600 and 0 <= y < 900
    assert isinstance(mask, int) and mask >= 0


@gnome
def test_nautilus_is_listed_and_activate_window_focuses_it(gnome_session, shell_helper):
    """Acceptance: Nautilus shows up with wm_class org.gnome.Nautilus; ActivateWindow
    moves the focus between two of its windows."""
    s, shell = gnome_session, shell_helper
    tag = secrets.token_hex(3)
    dirs = [s.root / "home" / f"helper-{n}-{tag}" for n in ("a", "b")]
    for d in dirs:
        d.mkdir()
    procs = []
    try:
        wins = []
        for d in dirs:
            procs.append(s.popen(["nautilus", "--new-window", str(d)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wins.append(shell.wait_for_window(wm_class="org.gnome.Nautilus", title=d.name))
        a, b = wins
        assert a["id"] != b["id"]
        for w in wins:
            assert w["client"] == "wayland" and w["xid"] == 0
            assert w["window_type"] == "normal" and w["pid"] > 0
            x, y, width, height = w["frame"]
            assert width > 0 and height > 0 and 0 <= x < 1600 and 0 <= y < 900
        for target, other in ((a, b), (b, a), (a, b)):
            assert shell.activate_window(target["id"]) is True
            assert _until(lambda: _focused(shell, target["id"])), f"{target['title']} not focused"
            assert not _focused(shell, other["id"])
        top = [w for w in shell.list_windows() if w["id"] in (a["id"], b["id"])]
        assert top[-1]["id"] == a["id"], "activated window is not on top of the stacking order"
        assert shell.activate_window(2**63) is False
    finally:
        s.run(["nautilus", "-q"], timeout=30)
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


@gnome
def test_x11_window_reports_its_xid(gnome_session, shell_helper):
    s, shell = gnome_session, shell_helper
    title = f"x11-probe-{secrets.token_hex(3)}"
    p, xid = _x11_window(s, title)
    try:
        w = shell.wait_for_window(title=title)
        assert (w["client"], w["xid"], w["pid"]) == ("x11", xid, p.pid)
        assert shell.activate_window(xid) is True       # by X11 id, not Meta id
        assert _until(lambda: _focused(shell, w["id"]))
    finally:
        p.kill()
        p.wait()


@gnome
def test_first_activation_after_boot_keeps_focus():
    """The headless shell boots into the overview. When the overview closes it gives the
    focus back to the window that had it, undoing an activation made meanwhile. The helper
    leaves the overview before it answers, so the first activation sticks."""
    require_session_tools()
    with Session(helper=True) as s, ShellHelper(s) as shell:
        procs, wins = [], []
        try:
            for name in ("older", "newer"):
                folder = s.root / "home" / name
                folder.mkdir()
                procs.append(s.popen(["nautilus", "--new-window", str(folder)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                wins.append(shell.wait_for_window(wm_class="org.gnome.Nautilus", title=name))
            time.sleep(1)  # let both windows map and settle
            assert shell.activate_window(wins[0]["id"])
            assert _until(lambda: _focused(shell, wins[0]["id"]))
            for _ in range(10):  # and keeps it (it used to fall back to "newer")
                time.sleep(0.1)
                assert _focused(shell, wins[0]["id"]), \
                    [(w["title"], w["focused"]) for w in shell.list_windows()]
        finally:
            s.run(["nautilus", "-q"], timeout=30)
            for p in procs:
                p.terminate()
                p.wait(timeout=10)


@gnome
def test_list_windows_is_empty_without_windows():
    """Plain GNOME mode: no DING desktop window, no apps -> [] rather than an error."""
    require_session_tools()
    with Session(helper=True, shell_args=["--mode=user"]) as s, ShellHelper(s) as shell:
        assert shell.list_windows() == []


@gnome
def test_broken_helper_fails_session_start_with_the_shell_log(tmp_path, monkeypatch):
    """Edge case: the helper does not load (syntax error) -> starting the session fails
    fast, the error names the cause from the shell log, and nothing is left behind."""
    require_session_tools()
    broken = tmp_path / UUID
    shutil.copytree(EXTENSION_DIR, broken)
    (broken / "extension.js").write_text("export default class {\n  enable( {\n}\n")
    monkeypatch.setattr(session_mod, "HELPER_EXTENSION", broken)
    s = Session(helper=True)
    t0 = time.monotonic()
    with pytest.raises(HelperError) as e:
        s.start()
    assert time.monotonic() - t0 < 15, "an extension in ERROR state should fail fast"
    msg = str(e.value)
    assert "State: ERROR" in msg and "--- gnome-shell log tail ---" in msg
    assert "SyntaxError" in msg, msg
    assert not s.root.exists() and not s.runtime_dir.exists()
