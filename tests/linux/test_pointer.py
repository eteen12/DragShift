"""GetPointer in the shipped extension and its client platform_linux.pointer_state() /
extension_available(): position, left button, modifier bits, the back-off when the
extension stops, and the call latency the 15 ms trigger timer pays.

Session tests load the extension only inside the isolated session from
tests/linux/session.py and drive the pointer with tests/linux/inject.py. The client runs
in this process against that session's private bus (DBUS_SESSION_BUS_ADDRESS is pointed
at it for the test), never the real desktop's.
"""
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from dragshift import platform_linux as lx
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (ROOT, UUID, _errors_about, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)

gnome = pytest.mark.gnome

NUM_LOCK = 1 << 4          # Clutter MOD2_MASK
SUPER = 1 << 26            # Clutter SUPER_MASK
MONITORS = ((1600, 900), (800, 600))    # the second one sits right of the first


def _reset_client():
    """Close the client's connections and forget its availability and back-off."""
    for conn in (lx._MAIN, lx._WORKER):
        if conn.bus is not None:
            try:
                conn.bus.close()
            except Exception:
                pass
        conn.bus = None
    lx._answered, lx._retry_at, lx._backoff = False, 0.0, 0.0


# ------------------------------------------------------------------ client, no bus
@pytest.fixture
def fake(monkeypatch):
    """lx._call replaced by a queue of replies (None = failed call), and a fake clock."""
    f = SimpleNamespace(now=1000.0, calls=0, replies=[])

    def call(method, *args, **kw):
        assert method == "GetPointer"
        f.calls += 1
        return f.replies.pop(0) if f.replies else None

    monkeypatch.setattr(lx, "_call", call)
    monkeypatch.setattr(lx, "time", SimpleNamespace(monotonic=lambda: f.now))
    for name, value in (("_answered", False), ("_retry_at", 0.0), ("_backoff", 0.0)):
        monkeypatch.setattr(lx, name, value)
    return f


def test_pointer_state_maps_the_reply(fake):
    fake.replies = [(321, 210, 0), (5, 6, lx.BUTTON1_MASK | lx.SHIFT_MASK | NUM_LOCK | SUPER)]
    assert lx.pointer_state() == (321, 210, False, 0)
    assert lx.extension_available() and fake.calls == 1    # no extra call once answered
    x, y, left, mask = lx.pointer_state()
    assert (x, y, left) == (5, 6, True)
    # other bits pass through; the app only ever tests KEY_MASKS / BUTTON1_MASK
    assert mask & lx.KEY_MASKS["shift"] and not mask & lx.KEY_MASKS["ctrl"]
    assert not mask & lx.KEY_MASKS["alt"]


def test_failures_back_off_and_a_success_resets(fake):
    assert lx.pointer_state() is None and fake.calls == 1
    assert not lx.extension_available() and fake.calls == 1      # still backing off
    waits = []
    for _ in range(7):
        start, calls = fake.now, fake.calls
        while fake.calls == calls:          # poll like the 15 ms trigger timer
            fake.now += 0.015
            assert lx.pointer_state() is None
        waits.append(round(fake.now - start, 1))
    assert waits == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    fake.now += 5.0
    fake.replies = [(1, 2, 0)]
    assert lx.pointer_state() == (1, 2, False, 0) and lx.extension_available()
    assert lx.pointer_state() is None                           # next failure: 0.5 s again
    fake.now += 0.49
    assert lx.pointer_state() is None and fake.calls == 10
    fake.now += 0.01
    lx.pointer_state()
    assert fake.calls == 11


def test_extension_available_probes_only_when_due(fake):
    assert not lx.extension_available() and fake.calls == 1
    assert not lx.extension_available() and fake.calls == 1
    fake.now += 0.5
    fake.replies = [(0, 0, 0)]
    assert lx.extension_available() and fake.calls == 2         # appeared later: noticed
    assert lx.extension_available() and fake.calls == 2


def test_malformed_reply_counts_as_a_failure(fake):
    fake.replies = [("1.1.0",)]
    assert lx.pointer_state() is None and not lx.extension_available()


@pytest.mark.skipif(not shutil.which("dbus-run-session"), reason="needs dbus-run-session")
def test_absent_and_hung_extension_cost_the_timer_one_call(tmp_path):
    """No owner → None at once. An owner that never answers → None after DBUS_TIMEOUT,
    then None at once (no D-Bus traffic) until the back-off has passed."""
    script = tmp_path / "hung.py"
    script.write_text(textwrap.dedent(f"""
        import time
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection
        from dragshift import platform_linux as p

        def timed(fn):
            t = time.perf_counter()
            v = fn()
            return v, (time.perf_counter() - t) * 1000

        print("absent", *timed(p.pointer_state))
        p._retry_at = 0.0
        conn = open_dbus_connection(bus="SESSION")   # owns the name, never replies
        conn.send_and_get_reply(message_bus.RequestName({lx.BUS_NAME!r}))
        print("hung", *timed(p.pointer_state))
        print("backoff", *timed(p.pointer_state))
        print("available", *timed(p.extension_available))
    """))
    r = subprocess.run(["dbus-run-session", "--", sys.executable, str(script)],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                            "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    rows = {line.split()[0]: line.split()[1:] for line in r.stdout.splitlines()}
    print(rows)
    assert rows["absent"][0] == "None" and float(rows["absent"][1]) < 150
    assert rows["hung"][0] == "None" and 150 < float(rows["hung"][1]) < 400
    assert rows["backoff"][0] == "None" and float(rows["backoff"][1]) < 5
    assert rows["available"][0] == "False" and float(rows["available"][1]) < 5


# ------------------------------------------------------------------ in the session
@pytest.fixture(scope="module")
def ptr_session(built_dir):  # noqa: F811
    require_session_tools()
    (w, h), (w2, h2) = MONITORS
    with Session(size=(w, h), extensions=[built_dir], helper=True,
                 shell_args=["--virtual-monitor", f"{w2}x{h2}"]) as s:
        yield s


@pytest.fixture(scope="module")
def shell(ptr_session):
    with ShellHelper(ptr_session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(ptr_session)), _state(ptr_session)
        yield helper


@pytest.fixture
def client(ptr_session, shell, monkeypatch):
    """platform_linux in this process, connected to the isolated session's bus."""
    _reset_client()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", ptr_session.bus_address)
    try:
        assert _wait(lambda: lx._call("GetPointer") is not None)
        yield lx
    finally:
        shell.release_all()
        _reset_client()


@gnome
def test_reports_injected_position(shell, client):
    """Acceptance: move to (321, 210) → (321, 210, False, 0)."""
    assert inject.move(shell, 321, 210) == (321, 210)
    assert client.pointer_state() == (321, 210, False, 0)
    assert client.extension_available()
    inject.glide(shell, 700, 450)
    assert client.pointer_state() == (700, 450, False, 0)


@gnome
def test_coordinates_are_global_across_monitors(shell, client):
    w = MONITORS[0][0]
    for x, y in ((w + 300, 300), (w + 799, 599), (w - 1, 899)):
        assert inject.move(shell, x, y) == (x, y)
        assert client.pointer_state() == (x, y, False, 0)


@gnome
def test_left_button(shell, client):
    inject.move(shell, 321, 210)
    inject.button_down(shell)
    x, y, left, mask = client.pointer_state()
    assert (x, y, left) == (321, 210, True) and mask & lx.BUTTON1_MASK
    inject.button_up(shell)
    assert client.pointer_state() == (321, 210, False, 0)


@gnome
@pytest.mark.parametrize("key, bit", [("Shift_L", lx.SHIFT_MASK), ("Shift_R", lx.SHIFT_MASK),
                                      ("Control_L", lx.CONTROL_MASK), ("Alt_L", lx.MOD1_MASK)])
def test_modifier_bits(shell, client, key, bit):
    inject.move(shell, 321, 210)
    with inject.hold_key(shell, key):
        assert client.pointer_state() == (321, 210, False, bit)
    assert client.pointer_state() == (321, 210, False, 0)


@gnome
def test_shift_drag_reports_button_modifier_and_motion(shell, client):
    """The input the trigger watches for: button held, Shift down, pointer moving."""
    inject.drag_to(shell, 600, 400, start=(321, 210), hold="Shift_L", release=False)
    x, y, left, mask = client.pointer_state()
    assert (x, y, left) == (600, 400, True)
    assert mask == lx.BUTTON1_MASK | lx.SHIFT_MASK
    inject.button_up(shell)
    shell.release_all()
    assert client.pointer_state() == (600, 400, False, 0)


@gnome
def test_stopping_the_extension_gives_none_then_recovers(ptr_session, shell, client):
    """Acceptance: the extension stops mid-test → None (no exception) within 200 ms; once
    it is back, polling at the trigger's 15 ms rate picks it up without a restart."""
    s = ptr_session
    inject.move(shell, 321, 210)
    assert client.pointer_state() is not None
    try:
        assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
        assert _wait(lambda: not _has_owner(s))
        t = time.perf_counter()
        assert client.pointer_state() is None
        first_ms = (time.perf_counter() - t) * 1000
        assert first_ms < 200
        assert not client.extension_available()
        polls = 0
        t = time.perf_counter()
        while time.perf_counter() - t < 1.0:       # the dead extension, at the timer's rate
            assert client.pointer_state() is None
            polls += 1
            time.sleep(0.015)
    finally:
        assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
    assert _wait(lambda: _has_owner(s))
    t = time.perf_counter()
    while client.pointer_state() is None:
        assert time.perf_counter() - t < lx.BACKOFF_MAX + 1, "did not recover"
        time.sleep(0.015)
    recovered_s = time.perf_counter() - t
    assert client.pointer_state() == (321, 210, False, 0) and client.extension_available()
    print(f"\nextension stopped: first call None after {first_ms:.2f} ms, {polls} polls "
          f"in 1 s all None; back on the bus → recovered after {recovered_s:.2f} s")


def _bench(fn, n):
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    return {"mean": statistics.mean(times), "p50": times[n // 2], "p99": times[n * 99 // 100],
            "max": times[-1]}


@gnome
def test_latency(shell, client):
    """Micro-benchmark: mean pointer_state() call time, idle and while the pointer moves
    (target < 2 ms; the trigger polls every 15 ms)."""
    n = 2000
    inject.move(shell, 321, 210)
    client.pointer_state()                         # connected and warm
    idle = _bench(client.pointer_state, n)
    stop = threading.Event()

    def wiggle():
        while not stop.is_set():
            inject.glide(shell, 900, 600, dur=0.3)
            inject.glide(shell, 321, 210, dur=0.3)

    mover = threading.Thread(target=wiggle)
    mover.start()
    try:
        moving = _bench(client.pointer_state, n)
    finally:
        stop.set()
        mover.join()
    for name, r in (("idle", idle), ("moving", moving)):
        print(f"\npointer_state() {name}, {n} calls: " +
              ", ".join(f"{k} {v:.3f} ms" for k, v in r.items()))
    assert idle["mean"] < 2 and moving["mean"] < 2


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(ptr_session, shell):
    assert _errors_about(ptr_session.log_path.read_text(errors="replace")) == []
