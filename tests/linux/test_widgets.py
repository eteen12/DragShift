"""AT-SPI widget lookup for driving DragShift's windows (tests/linux/widgets.py, #61).

In the isolated session: a real tool window (tools.ResizeWindow, native Wayland) exports
its widgets once accessibility is turned on, each box plus the ListWindows frame is where
the widget really is (an injected click on it toggles that preset), and press() runs a
button's action. Without a session: the tree walk and the client's error handling."""
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.linux import inject
from tests.linux import widgets as widgets_mod
from tests.linux.widgets import Widgets, WidgetsError, match

REPO = widgets_mod.Path(__file__).resolve().parents[2]
TITLE = "Resize — DragShift"

APP = textwrap.dedent("""
    import sys
    from PySide6.QtWidgets import QApplication
    from dragshift import tools
    app = QApplication(sys.argv)
    w = tools.ResizeWindow([sys.argv[1]], lambda *a: None)
    w.show()
    print("ready", flush=True)
    app.exec()
""")


def _wait(fn, timeout=10.0, step=0.1):
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v or time.monotonic() > end:
            return v
        time.sleep(step)


@pytest.fixture
def resize_window(gnome_session, shell_helper):
    img = gnome_session.root / "tmp" / "widgets.png"
    Image.new("RGB", (400, 200), "white").save(img)
    proc = gnome_session.popen([sys.executable, "-c", APP, str(img)], cwd=REPO,
                               env={"QT_QPA_PLATFORM": "wayland", "PYTHONPATH": str(REPO)},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        win = _wait(lambda: [w for w in shell_helper.list_windows() if w["title"] == TITLE])
        assert win, "the Resize window never appeared"
        yield win[0]
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.mark.gnome
def test_widgets_of_a_real_tool_window(gnome_session, shell_helper, resize_window):
    w = Widgets(gnome_session)
    assert w.available()
    w.enable()
    found = _wait(lambda: _try(w.find, TITLE))
    assert found, "Qt never exported the window"
    presets = {it["name"]: it for it in found if it["role"] == "check box"}
    assert {"25%", "50%", "1920"} <= set(presets) and presets["50%"]["checked"]
    resize = match(found, "Resize", "push button")
    assert len(resize) == 1 and resize[0]["enabled"]
    # The box (window coordinates) plus the frame is where the widget is on the stage.
    frames, end = [], time.monotonic() + 5
    while (len(frames) < 3 or len(set(frames[-3:])) > 1) and time.monotonic() < end:
        time.sleep(0.1)
        frames += [tuple(x["frame"]) for x in shell_helper.list_windows() if x["title"] == TITLE]
    assert frames, "the Resize window went away"
    x0, y0 = frames[-1][:2]
    x, y, bw, bh = presets["25%"]["box"]
    shell_helper.activate_window(resize_window["id"])
    inject.click(shell_helper, x0 + x + bw // 2, y0 + y + bh // 2)
    assert _wait(lambda: match(w.find(TITLE), "25%")[0]["checked"], 5)
    assert not match(w.find(TITLE), "50%")[0]["checked"]
    with pytest.raises(WidgetsError, match="no accessible window"):
        w.find("Nothing — DragShift")
    w.press(TITLE, "✕")                          # the close button's action
    assert _wait(lambda: not [x for x in shell_helper.list_windows() if x["title"] == TITLE], 5)


def _try(fn, *a):
    try:
        return fn(*a)
    except WidgetsError:
        return None


# ------------------------------------------------------------------ no session
STATE = SimpleNamespace(SHOWING=1, VISIBLE=2, CHECKED=3, ENABLED=4, FOCUSED=5)
FAKE_ATSPI = SimpleNamespace(CoordType=SimpleNamespace(WINDOW=1), StateType=STATE)
SHOWN = {STATE.SHOWING, STATE.VISIBLE, STATE.ENABLED}


class FakeNode:
    """A minimal Atspi.Accessible stand-in for the tree walk."""

    def __init__(self, role, name="", box=(0, 0, 0, 0), states=SHOWN, children=()):
        self.role, self.name, self.box = role, name, box
        self.states, self.children = set(states), list(children)

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_extents(self, _coord):
        x, y, w, h = self.box
        return SimpleNamespace(x=x, y=y, width=w, height=h)

    def get_state_set(self):
        return SimpleNamespace(contains=lambda s: s in self.states)


def test_walk_keeps_showing_widgets_in_tree_order():
    frame = FakeNode("frame", TITLE, children=[
        FakeNode("label", "Resize", (14, 8, 60, 22)),
        FakeNode("filler", "", (14, 40, 500, 40), children=[
            FakeNode("check box", "25%", (14, 40, 60, 32)),
            FakeNode("check box", "50%", (80, 40, 60, 32), SHOWN | {STATE.CHECKED}),
        ]),
        FakeNode("filler", "", (14, 90, 500, 40), states={STATE.ENABLED}, children=[
            FakeNode("check box", "200 KB", (14, 90, 60, 32)),   # under a hidden parent
        ]),
        FakeNode("push button", "Resize", (500, 300, 90, 34), {STATE.SHOWING, STATE.VISIBLE}),
    ])
    got = widgets_mod._walk(frame, FAKE_ATSPI)
    assert [(g["role"], g["name"]) for g in got] == [
        ("label", "Resize"), ("filler", ""), ("check box", "25%"), ("check box", "50%"),
        ("push button", "Resize")]
    assert got[3]["checked"] and not got[2]["checked"]
    assert got[2]["box"] == [14, 40, 60, 32]
    assert not got[4]["enabled"] and got[2]["enabled"]
    assert match(got, "Resize", "push button") == [got[4]]
    assert match(got, "Resize") == [got[0], got[4]]


def test_frame_lookup_by_exact_title():
    app = FakeNode("application", "DragShift", children=[
        FakeNode("frame", "Resize (2) — DragShift"), FakeNode("frame", TITLE)])
    desktop = FakeNode("desktop", children=[FakeNode("application", "gnome-shell"), app])
    atspi = SimpleNamespace(get_desktop=lambda _i: desktop)
    frame, titles = widgets_mod._frame(atspi, TITLE)
    assert frame is app.children[1]
    assert widgets_mod._frame(atspi, "Resize")[0] is None
    assert titles == ["Resize (2) — DragShift", TITLE]


class FakeSession:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.reply = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
        self.cmds = []

    def run(self, cmd, **kw):
        self.cmds.append(cmd)
        return self.reply


def test_client_reports_bridge_errors(monkeypatch):
    monkeypatch.setattr("tests.linux.session.gi_python", lambda: "/usr/bin/python3")
    s = FakeSession('{"widgets": [{"name": "OK"}]}\n')
    assert Widgets(s).find(TITLE) == [{"name": "OK"}]
    assert s.cmds[0][:3] == ["/usr/bin/python3", "-I", str(widgets_mod.Path(widgets_mod.__file__).resolve())]
    with pytest.raises(WidgetsError, match="no accessibility bus"):
        Widgets(FakeSession('{"error": "no accessibility bus (x)"}')).enable()
    with pytest.raises(WidgetsError, match="exited 134"):
        Widgets(FakeSession("", 134, "atspi abort")).find(TITLE)
    assert not Widgets(FakeSession("", 134)).available()
    monkeypatch.setattr("tests.linux.session.gi_python", lambda: None)
    with pytest.raises(WidgetsError, match="python3-gi"):
        Widgets(FakeSession()).find(TITLE)
