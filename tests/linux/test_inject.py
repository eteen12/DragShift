"""Pointer and keyboard injection in the isolated session (tests/linux/inject.py and the
helper extension's MovePointer/Button/Key/TypeText), checked against the probe window,
and proof that none of it reaches the real desktop."""
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image

from tests.linux import inject
from tests.linux import session as session_mod
from tests.linux.conftest import REAL_ENV, require_session_tools
from tests.linux.probe_window import ProbeWindow
from tests.linux.session import Session
from tests.linux.shell import BUS_NAME, HelperError, ShellHelper

gnome = pytest.mark.gnome

SHIFT_MASK = 1 << 0     # Clutter.ModifierType
BUTTON1_MASK = 1 << 8
STAGE = (1600, 900)


@pytest.fixture
def probe(gnome_session, shell_helper):
    with ProbeWindow(gnome_session, shell_helper) as p:
        p.focus()
        yield p
    shell_helper.release_all()


def _moves_at(probe, x, y, after=0):
    return probe.wait_for("mouseMove", lambda r: probe.screen_pos(r) == (x, y), after=after)


# ------------------------------------------------------------------ acceptance
@gnome
def test_move_pointer_is_reported_by_the_shell_and_the_probe(shell_helper, probe):
    """Acceptance: MovePointer(500, 400) -> GetPointer is (500, 400) and the probe logs
    a move at the same coordinates."""
    x, y, w, h = probe.frame()
    assert x <= 500 < x + w and y <= 400 < y + h, f"probe {probe.frame()} misses (500, 400)"
    shell_helper.move_pointer(10, 10)
    mark = probe.mark()
    assert shell_helper.move_pointer(500, 400) == (500, 400)
    assert shell_helper.get_pointer()[:2] == (500, 400)
    _moves_at(probe, 500, 400, after=mark)


@gnome
@pytest.mark.parametrize("target, expected", [
    ((5000, -300), (STAGE[0] - 1, 0)),
    ((-50, 2000), (0, STAGE[1] - 1)),
    ((2000, 2000), (STAGE[0] - 1, STAGE[1] - 1)),
])
def test_coordinates_outside_the_monitor_are_clamped(shell_helper, target, expected):
    # Not (0, 0): GNOME's hot corner there opens the overview where it is enabled (it is
    # in the Docker image's "user" mode; Ubuntu turns it off).
    assert shell_helper.move_pointer(*target) == expected
    x, y, _ = shell_helper.get_pointer()
    assert (x, y) == expected and 0 <= x < STAGE[0] and 0 <= y < STAGE[1]


@gnome
@pytest.mark.parametrize("button, name", [("left", "LeftButton"), ("right", "RightButton"),
                                          ("middle", "MiddleButton")])
def test_button_press_and_release_reach_the_probe(shell_helper, probe, button, name):
    cx, cy = probe.center(-100, 50)
    mark = probe.mark()
    inject.click(shell_helper, cx, cy, button=button)
    press = probe.wait_for("mousePress", lambda r: r["button"] == name, after=mark)
    release = probe.wait_for("mouseRelease", lambda r: r["button"] == name, after=mark)
    assert probe.screen_pos(press) == probe.screen_pos(release) == (cx, cy)
    assert press["buttons"] == [name] and release["buttons"] == []
    assert shell_helper.get_pointer()[2] & BUTTON1_MASK == 0


@gnome
def test_shift_held_during_a_move_is_seen_as_a_modifier(shell_helper, probe):
    """Acceptance: Key(Shift_L) held + move -> the probe logs ShiftModifier."""
    shell_helper.move_pointer(*probe.center())
    mark = probe.mark()
    with inject.hold_key(shell_helper, "Shift_L"):
        assert shell_helper.get_pointer()[2] & SHIFT_MASK
        shell_helper.move_pointer(*probe.center(40, 20))
    after_release = probe.mark()
    shell_helper.move_pointer(*probe.center(80, 40))
    probe.wait_for("keyPress", lambda r: r["key"] == "Key_Shift", after=mark)
    moved = _moves_at(probe, *probe.center(40, 20), after=mark)
    assert moved["modifiers"] == ["ShiftModifier"]
    probe.wait_for("keyRelease", lambda r: r["key"] == "Key_Shift", after=mark)
    later = _moves_at(probe, *probe.center(80, 40), after=after_release)
    assert later["modifiers"] == []
    assert shell_helper.get_pointer()[2] & SHIFT_MASK == 0


@gnome
def test_type_text_fills_the_focused_line_edit(shell_helper, probe):
    """Acceptance: TypeText("abc") into the probe's focused line edit yields "abc"."""
    inject.type_text(shell_helper, "abc")
    assert probe.wait_for("text", lambda r: r["text"] == "abc")
    inject.type_text(shell_helper, " Hi, 42!")   # shifted characters get Shift by themselves
    probe.wait_for("text", lambda r: r["text"] == "abc Hi, 42!")
    inject.press(shell_helper, "BackSpace")
    probe.wait_for("text", lambda r: r["text"] == "abc Hi, 42")
    assert shell_helper.get_pointer()[2] & SHIFT_MASK == 0


@gnome
@pytest.mark.parametrize("key, qt", [("Escape", "Key_Escape"), ("Return", "Key_Return"),
                                     ("Tab", "Key_Tab"), ("Control_L", "Key_Control"),
                                     ("Alt_L", "Key_Alt")])
def test_press_sends_keysyms(shell_helper, probe, key, qt):
    mark = probe.mark()
    inject.press(shell_helper, key)
    probe.wait_for("keyPress", lambda r: r["key"] == qt, after=mark)
    probe.wait_for("keyRelease", lambda r: r["key"] == qt, after=mark)


# ------------------------------------------------------------------ edge cases
@gnome
def test_shift_stays_down_across_a_drag_until_released(shell_helper, probe):
    """Edge case: a modifier held across a drag stays down until released explicitly."""
    start, end = probe.center(-250, -100), probe.center(200, 120)
    mark = probe.mark()
    inject.drag_to(shell_helper, *end, start=start, hold="Shift_L", release=False)
    time.sleep(0.3)
    assert shell_helper.get_pointer()[2] & (SHIFT_MASK | BUTTON1_MASK) == \
        SHIFT_MASK | BUTTON1_MASK
    inject.button_up(shell_helper)
    rel = probe.wait_for("mouseRelease", after=mark)
    assert rel["modifiers"] == ["ShiftModifier"], "Shift was released before the drop"
    assert shell_helper.get_pointer()[2] & SHIFT_MASK, "Shift released by the button"
    shell_helper.key(inject.KEYSYMS["Shift_L"], False)
    assert shell_helper.get_pointer()[2] & SHIFT_MASK == 0
    drag = [r for r in probe.events("mouseMove")[mark:] if r["buttons"] == ["LeftButton"]]
    assert len(drag) >= inject.GLIDE_STEPS
    shifted = [r for r in drag if r["modifiers"] == ["ShiftModifier"]]
    assert shifted and probe.screen_pos(shifted[-1]) == end


@gnome
def test_release_all_lets_go_of_everything(shell_helper, probe):
    shell_helper.move_pointer(*probe.center())
    mark = probe.mark()
    inject.button_down(shell_helper)
    shell_helper.key(inject.KEYSYMS["Shift_L"], True)
    shell_helper.key(inject.KEYSYMS["Control_L"], True)
    assert shell_helper.get_pointer()[2] & (SHIFT_MASK | BUTTON1_MASK)
    inject.release_all(shell_helper)
    assert shell_helper.get_pointer()[2] & (SHIFT_MASK | BUTTON1_MASK | 1 << 2) == 0
    probe.wait_for("mouseRelease", after=mark)
    probe.wait_for("keyRelease", lambda r: r["key"] == "Key_Shift", after=mark)
    probe.wait_for("keyRelease", lambda r: r["key"] == "Key_Control", after=mark)


@gnome
def test_injected_drag_is_drawn_by_the_probe(gnome_session, shell_helper, probe):
    """A glide-drag with the primary button arrives as press, held moves and release,
    and the probe paints its stroke (the screenshot is the evidence)."""
    start, end = probe.center(-300, -120), probe.center(260, 150)
    mark = probe.mark()
    inject.drag_to(shell_helper, *end, start=start, dur=0.5)
    press = probe.wait_for("mousePress", after=mark)
    release = probe.wait_for("mouseRelease", after=mark)
    assert probe.screen_pos(press) == start and probe.screen_pos(release) == end
    held = [r for r in probe.events("mouseMove")[mark:] if r["buttons"] == ["LeftButton"]]
    assert len(held) >= inject.GLIDE_STEPS
    xs = [probe.screen_pos(r)[0] for r in held]
    assert xs == sorted(xs), "the drag went backwards"
    time.sleep(0.3)  # let the probe repaint
    shot = shell_helper.screenshot(gnome_session.root / "tmp" / "inject-drag.png")
    mid = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
    with Image.open(shot) as im:
        rgb = im.convert("RGB")
        # The stroke is #1f5fbf, 4 px wide; look in a small box around its midpoint.
        blue = [rgb.getpixel((mid[0] + dx, mid[1] + dy))
                for dx in range(-4, 5) for dy in range(-4, 5)]
    assert any(b > 150 and r < 80 and g < 130 for r, g, b in blue), blue[:5]
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"], "inject-drag.png").write_bytes(
            shot.read_bytes())


@gnome
def test_xcb_probe_sees_global_coordinates(gnome_session, shell_helper):
    """Under Xwayland, Qt reports global positions itself; they must equal the injected
    stage coordinates. Skipped where Qt cannot load xcb (no libxcb-cursor0)."""
    from tests.linux.probe_window import qt_platform
    if qt_platform(gnome_session) != "xcb":
        pytest.skip("Qt xcb plugin does not load here (libxcb-cursor0 missing)")
    with ProbeWindow(gnome_session, shell_helper, platform="xcb") as probe:
        probe.focus()
        x, y, w, h = probe.frame()
        assert probe.window["client"] == "x11" and probe.window["xid"] == probe.ready["xid"]
        mark = probe.mark()
        assert shell_helper.move_pointer(x + 100, y + 200) == (x + 100, y + 200)
        r = probe.wait_for("mouseMove", lambda r: (r["x"], r["y"]) == (x + 100, y + 200),
                           after=mark)
        assert (r["lx"], r["ly"]) == (100, 200)
        inject.type_text(shell_helper, "abc")
        probe.wait_for("text", lambda r: r["text"] == "abc")


# ------------------------------------------------------------------ isolation
def _owner_pid(session, name):
    r = session.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                     "--object-path", "/org/freedesktop/DBus", "--method",
                     "org.freedesktop.DBus.GetConnectionUnixProcessID", name], check=True)
    return int(re.search(r"\d+", r.stdout.split()[-1]).group())


@gnome
def test_injection_runs_inside_the_isolated_shell(gnome_session, shell_helper):
    """The input methods are served by the isolated gnome-shell on the session's private
    bus, and the client talks to that bus, never the real one."""
    assert gnome_session.bus_address != REAL_ENV["DBUS_SESSION_BUS_ADDRESS"]
    assert _owner_pid(gnome_session, BUS_NAME) == gnome_session.shell_pid
    environ = Path(f"/proc/{shell_helper._bridge.pid}/environ").read_bytes().split(b"\0")
    assert f"DBUS_SESSION_BUS_ADDRESS={gnome_session.bus_address}".encode() in environ
    shell_env = Path(f"/proc/{gnome_session.shell_pid}/environ").read_bytes().split(b"\0")
    assert f"XDG_RUNTIME_DIR={gnome_session.runtime_dir}".encode() in shell_env


@gnome
def test_isolated_session_holds_no_input_hardware(gnome_session, shell_helper):
    """The headless shell's seat has no libinput devices: no process of the session has
    /dev/input/* or /dev/uinput open, so its virtual devices only exist in-process."""
    shell_helper.move_pointer(300, 300)
    pids = session_mod._pids_with_runtime_dir(str(gnome_session.runtime_dir))
    assert gnome_session.shell_pid in pids
    opened = []
    for pid in pids:
        try:
            fds = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/dev/input/") or target == "/dev/uinput":
                opened.append((pid, target))
    assert opened == []


def _kernel_input_devices() -> str:
    return Path("/proc/bus/input/devices").read_text()


@gnome
def test_injection_creates_no_kernel_input_device(shell_helper):
    """No uinput device appears while injecting (it would be visible to the real
    desktop's libinput)."""
    before = _kernel_input_devices()
    inject.glide(shell_helper, 800, 450, dur=0.2)
    inject.click(shell_helper, 820, 460)
    inject.press(shell_helper, "Escape")
    inject.type_text(shell_helper, "x")
    assert _kernel_input_devices() == before


def _real_idle_ms() -> int | None:
    """Idle time of the real desktop's GNOME session (org.gnome.Mutter.IdleMonitor, a
    read-only call on the real session bus), or None without one."""
    bus = REAL_ENV["DBUS_SESSION_BUS_ADDRESS"]
    if not bus:
        return None
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "HOME")}
    r = subprocess.run(["gdbus", "call", "--session", "--timeout", "2", "--dest",
                        "org.gnome.Mutter.IdleMonitor", "--object-path",
                        "/org/gnome/Mutter/IdleMonitor/Core", "--method",
                        "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                       env=dict(env, DBUS_SESSION_BUS_ADDRESS=bus),
                       capture_output=True, text=True)
    m = re.search(r"uint64 (\d+)", r.stdout)
    return int(m.group(1)) if r.returncode == 0 and m else None


@gnome
def test_real_desktop_receives_no_input(shell_helper, probe):
    """Every input event the real desktop's compositor handles resets its idle time, so
    an idle time that keeps growing through two seconds of injected moves, clicks and
    keys shows that none of them reached the real seat (and the real pointer did not
    move). Skipped without a real GNOME session (CI) or while someone is using it."""
    idle0 = _real_idle_ms()
    if idle0 is None:
        pytest.skip("no real GNOME session bus to compare against")
    if idle0 < 3000:
        pytest.skip(f"the real desktop is in use (idle {idle0} ms); input would be ambiguous")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        inject.glide(shell_helper, *probe.center(-200, -100), dur=0.15)
        inject.click(shell_helper, *probe.center(150, 80), dur=0.15)
        with inject.hold_key(shell_helper, "Shift_L"):
            inject.glide(shell_helper, *probe.center(), dur=0.1)
        inject.type_text(shell_helper, "q")
    elapsed_ms = (time.monotonic() - t0) * 1000
    idle1 = _real_idle_ms()
    assert idle1 is not None
    assert idle1 >= idle0 + elapsed_ms - 250, \
        f"real desktop idle time went from {idle0} to {idle1} ms during injection"


@gnome
def test_input_is_refused_outside_a_launcher_session(monkeypatch):
    """Second line of defence: the helper creates no virtual devices unless it runs in a
    launcher session root, so loaded anywhere else its input methods fail."""
    require_session_tools()
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, prefix=None, **kw):
        if prefix == "dragshift-session-":
            prefix = "dragshift-elsewhere-"
        return real_mkdtemp(*args, prefix=prefix, **kw)

    monkeypatch.setattr(session_mod.tempfile, "mkdtemp", mkdtemp)
    with Session(helper=True) as s, ShellHelper(s) as shell:
        assert s.root.name.startswith("dragshift-elsewhere-")
        before = shell.get_pointer()
        for call in (lambda: shell.move_pointer(10, 10), lambda: shell.button(1, True),
                     lambda: shell.key(0xffe1, True), lambda: shell.type_text("a"),
                     shell.release_all):
            with pytest.raises(HelperError, match="AccessDenied.*input injection refused"):
                call()
        assert shell.get_pointer() == before
        assert "input injection disabled" in s.log_path.read_text(errors="replace")


# ------------------------------------------------------------------ primitives (no session)
class FakeShell:
    """Records helper calls; the pointer follows move_pointer."""

    def __init__(self, x=0, y=0):
        self.pos = (x, y)
        self.calls = []

    def get_pointer(self):
        return (*self.pos, 0)

    def move_pointer(self, x, y):
        self.pos = (int(round(x)), int(round(y)))
        self.calls.append(("move", *self.pos))
        return self.pos

    def button(self, b, pressed):
        self.calls.append(("button", b, pressed))

    def key(self, k, pressed):
        self.calls.append(("key", k, pressed))

    def type_text(self, text):
        self.calls.append(("type", text))

    def release_all(self):
        self.calls.append(("release_all",))


def test_keysym_mapping():
    assert inject.keysym("Shift_L") == 0xffe1
    assert inject.keysym("Escape") == 0xff1b
    assert inject.keysym("Return") == 0xff0d
    assert inject.keysym("Control_L") == 0xffe3 and inject.keysym("Alt_L") == 0xffe9
    assert inject.keysym("a") == 0x61 and inject.keysym("A") == 0x41
    assert inject.keysym("é") == 0xe9                     # Latin-1: the code point
    assert inject.keysym("€") == 0x01000000 + 0x20ac      # others: 0x01000000 + code point
    assert inject.keysym(0xff09) == 0xff09
    with pytest.raises(ValueError):
        inject.keysym("NoSuchKey")


def test_glide_eases_in_at_least_60_steps():
    shell = FakeShell(100, 100)
    assert inject.glide(shell, 700, 400, dur=0) == (700, 400)
    moves = [c[1:] for c in shell.calls]
    assert len(moves) >= 60 and moves[-1] == (700, 400)
    steps = [b[0] - a[0] for a, b in zip([(100, 100)] + moves, moves)]
    assert steps[0] < steps[len(steps) // 2] > steps[-1]     # smoothstep: slow, fast, slow
    xs = [m[0] for m in moves]
    assert xs == sorted(xs)


def test_glide_takes_about_dur():
    t0 = time.monotonic()
    inject.glide(FakeShell(), 100, 100, dur=0.3)
    assert 0.28 <= time.monotonic() - t0 < 0.6


def test_drag_to_order_and_held_key():
    shell = FakeShell(0, 0)
    inject.drag_to(shell, 300, 0, start=(100, 0), dur=0, hold="Shift_L")
    kinds = [c[0] for c in shell.calls]
    down = shell.calls.index(("button", 1, True))
    shift_down = shell.calls.index(("key", 0xffe1, True))
    up = shell.calls.index(("button", 1, False))
    shift_up = shell.calls.index(("key", 0xffe1, False))
    assert down < shift_down < up < shift_up
    nudge = [c for c in shell.calls[down:shift_down] if c[0] == "move"]
    assert nudge[-1][1:] == (100 + inject.DRAG_NUDGE, 0)
    assert shell.calls[up - 1] == ("move", 300, 0)
    assert kinds.count("move") >= 2 * inject.GLIDE_STEPS


def test_drag_to_without_release_keeps_everything_down():
    shell = FakeShell()
    inject.drag_to(shell, 50, 50, dur=0, hold=("Shift_L", "Control_L"), release=False)
    assert ("button", 1, False) not in shell.calls
    assert all(c[2] for c in shell.calls if c[0] == "key")


def test_hold_key_releases_in_reverse_even_on_error():
    shell = FakeShell()
    with pytest.raises(RuntimeError):
        with inject.hold_key(shell, "Control_L", "Shift_L"):
            raise RuntimeError("boom")
    assert shell.calls == [("key", 0xffe3, True), ("key", 0xffe1, True),
                           ("key", 0xffe1, False), ("key", 0xffe3, False)]


def test_click_and_press():
    shell = FakeShell()
    inject.click(shell, 10, 20, button="right", count=2, dur=0)
    assert shell.calls[-4:] == [("button", 3, True), ("button", 3, False)] * 2
    assert shell.pos == (10, 20)
    shell.calls.clear()
    inject.press(shell, "Return")
    assert shell.calls == [("key", 0xff0d, True), ("key", 0xff0d, False)]
