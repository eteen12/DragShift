"""Only the DragShift app may call the extension's methods (#93).

GetPointer, CaptureArea, CaptureAreaExcludingCaller, WatchEscape and PlaceWindow answer a
same-user process whose command line runs the DragShift app (`python -m dragshift`,
`.../dragshift/__main__.py`, the ~/.local/bin/dragshift launcher) and refuse everyone
else with AccessDenied; Version stays open. Inside the test launcher's session root
(<tmp>/dragshift-session-*) the test processes are let in too: pytest, `-m tests.linux.*`
and scripts in tests/linux. The rules live in extension/.../caller.js.

- No shell: the command-line rules and the /proc checks, run in gjs against caller.js.
- In a test session: gdbus, `python -c` and a plain script are refused for every
  protected method; the test helpers and the real app (a Shift+drag opens its menu) work.
- In a session outside a session root (no test allowance): pytest and the test helpers
  are refused too, and a `python -m dragshift` command line is let in, which also shows
  the limit of the check (a same-user process can start one).
- The latency of GetPointer once the verdict is cached.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from jeepney.io.blocking import open_dbus_connection

from dragshift.trigger import LINUX_DRAG_THRESHOLD
from tests.linux import caller, inject
from tests.linux import session as session_mod
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (ROOT, SRC, _errors_about, _has_owner, _state,  # noqa: F401
                                        _wait, built_dir, built_zip)

gnome = pytest.mark.gnome

DENIED = "org.freedesktop.DBus.Error.AccessDenied"
ALL_OK = {m: "ok" for m in ("Version", *caller.PROTECTED)}
ALL_DENIED = {"Version": "ok", **{m: DENIED for m in caller.PROTECTED}}
# gdbus arguments for each protected method (as in tests/linux/caller.py)
GDBUS_ARGS = {"GetPointer": [], "CaptureArea": ["0", "0", "10", "10"],
              "CaptureAreaExcludingCaller": ["0", "0", "10", "10"],
              "WatchEscape": ["false"], "PlaceWindow": ["no such window", "0", "0"],
              "GetWorkArea": ["0", "0"], "GetOwnWindowFrame": ["no such window"]}
CALLER_SRC = Path(caller.__file__).read_text()


# ------------------------------------------------------------------ no shell (gjs)
GJS = """
import * as c from '%s';
const out = {};
for (const argv of JSON.parse(ARGV[0]))
    out[JSON.stringify(argv)] = [c.isDragShiftCommand(argv, '/h'), c.isTestCommand(argv)];
for (const pid of JSON.parse(ARGV[1]))
    out[pid] = c.refusal(pid, '/h', false);
print(JSON.stringify(out));
"""

# argv -> (is the DragShift app, is a test process)
COMMANDS = [
    (["/h/.local/share/dragshift/venv/bin/python", "-m", "dragshift", "--quiet"], True, False),
    (["python3", "-m", "dragshift"], True, False),
    (["python3", "-Im", "dragshift"], True, False),
    (["python3.12", "-X", "dev", "-W", "ignore", "-m", "dragshift", "a.png"], True, False),
    (["/usr/bin/python3", "/src/DragShift/dragshift/__main__.py"], True, False),
    (["python3", "dragshift/__main__.py"], True, False),
    (["/venv/bin/python", "/h/.local/bin/dragshift", "a.png"], True, False),  # launcher script
    (["python3", "-c", "import x", "-m", "dragshift"], False, False),
    (["python3", "-m", "dragshift.app"], False, False),
    (["python3", "-m", "dragshiftx"], False, False),
    (["python3", "run.py"], False, False),
    (["python3", "/tmp/notdragshift/__main__.py"], False, False),
    (["python3", "/other/.local/bin/dragshift"], False, False),
    (["/bin/sh", "/h/.local/bin/dragshift"], False, False),
    (["/usr/bin/gdbus", "call", "-m", "dragshift"], False, False),
    (["pythonista", "-m", "dragshift"], False, False),
    (["python3"], False, False),
    (["python3", "-"], False, False),
    (["python3", "-m"], False, False),
    (["python3", "-m", "pytest", "-q"], False, True),
    (["/v/bin/python", "/v/bin/pytest", "-m", "gnome"], False, True),
    (["python", "-m", "tests.linux.caller"], False, True),
    (["python", "-I", "/r/tests/linux/menu_probe.py", "--log", "x"], False, True),
    (["python", "-c", "import pytest"], False, False),
    (["python", "/tmp/probe.py"], False, False),
    (["python", "-m", "tests.linux"], False, False),
    (["python", "/r/tests/linux/sub/x.py"], False, False),
]


def _gjs(argvs, pids):
    if not shutil.which("gjs"):
        pytest.skip("gjs not installed")
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "t.mjs"
        script.write_text(GJS % (SRC / "caller.js").as_uri())
        r = subprocess.run(["gjs", "-m", str(script), json.dumps(argvs), json.dumps(pids)],
                           capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_command_line_rules():
    got = _gjs([argv for argv, _, _ in COMMANDS], [])
    for argv, app, test in COMMANDS:
        assert got[json.dumps(argv, separators=(",", ":"))] == [app, test], argv


def _fake_app(where: Path) -> Path:
    """tests/linux/caller.py saved as <where>/dragshift/__main__.py, so `python -m
    dragshift` run in `where` is this probe with the DragShift app's command line. It is
    a regular package (__init__.py): a namespace package would lose to the real one if
    the repo were on the path, and the real app would start instead."""
    (where / "dragshift").mkdir(parents=True)
    (where / "dragshift" / "__init__.py").write_text("")
    (where / "dragshift" / "__main__.py").write_text(CALLER_SRC)
    return where


def test_proc_checks_on_real_processes(tmp_path):
    """refusal() reads /proc/<pid>/exe and cmdline: a Python running `-m dragshift` is let
    in; Python running something else, a non-Python program and a gone pid are not."""
    _fake_app(tmp_path)
    (tmp_path / "dragshift" / "__main__.py").write_text("import time; time.sleep(60)\n")
    env = {"PATH": os.environ["PATH"]}        # nothing else on the path but the fake
    procs = {"app": subprocess.Popen([sys.executable, "-m", "dragshift"], cwd=tmp_path,
                                     env=env),
             "python": subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]),
             "sleep": subprocess.Popen(["sleep", "60"])}
    gone = subprocess.Popen(["true"])
    gone.wait()
    try:
        time.sleep(0.3)                    # let the interpreters reach their command line
        got = _gjs([], [p.pid for p in procs.values()] + [gone.pid])
    finally:
        for p in procs.values():
            p.kill()
            p.wait()
    assert got[str(procs["app"].pid)] is None
    assert got[str(procs["python"].pid)] == "not the DragShift app"
    assert got[str(procs["sleep"].pid)].endswith("is not a Python interpreter")
    assert got[str(gone.pid)].startswith(f"cannot read /proc/{gone.pid}")


# ------------------------------------------------------------------ helpers
def _gdbus(s, method, args=()):
    return s.run(["gdbus", "call", "--session", "--timeout", "5", "--dest", caller.BUS_NAME,
                  "--object-path", caller.OBJECT_PATH, "--method",
                  f"{caller.INTERFACE}.{method}", "--", *args])


def _in_process(s) -> dict:
    """caller.call_all() from this (pytest) process on the session's bus."""
    conn = open_dbus_connection(bus=s.bus_address)
    try:
        return caller.call_all(conn)
    finally:
        conn.close()


def _run(s, cmd, cwd=ROOT, pythonpath=str(ROOT)) -> dict:
    """call_all() from a process started with command line `cmd`."""
    r = s.run(cmd, cwd=cwd, env={"PYTHONPATH": pythonpath}, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _refusals(s, pid: int) -> list[str]:
    return [line for line in s.log_path.read_text(errors="replace").splitlines()
            if "DragShift: refused" in line and f"(pid {pid})" in line]


def _ready(s):
    assert _wait(lambda: _has_owner(s)), _state(s)


# ------------------------------------------------------------------ in a test session
@pytest.fixture(scope="module")
def cc_session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(size=(1280, 800), extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(cc_session):
    with ShellHelper(cc_session) as helper:
        helper.wait_ready()
        _ready(cc_session)
        yield helper


@gnome
def test_gdbus_is_refused_and_version_answers(cc_session, shell):
    s = cc_session
    for method, args in GDBUS_ARGS.items():
        r = _gdbus(s, method, args)
        assert r.returncode != 0 and DENIED in r.stderr, (method, r.stdout, r.stderr)
        assert f"{method} only answers the DragShift app" in r.stderr, r.stderr
    r = _gdbus(s, "Version")
    assert r.returncode == 0 and r.stdout.startswith("('"), r.stderr


@gnome
def test_plain_python_is_refused(cc_session, shell, tmp_path):
    """`python -c` and a plain script (the same probe as the helper module) are refused
    for every protected method, Version answers; the shell logs one line per caller."""
    s = cc_session
    code = "import sys; from tests.linux.caller import main; sys.exit(main(['x']))"
    assert _run(s, [sys.executable, "-c", code]) == ALL_DENIED
    script = tmp_path / "probe.py"
    script.write_text(CALLER_SRC)
    proc = s.popen([sys.executable, str(script)], cwd=tmp_path, stdout=subprocess.PIPE,
                   text=True, env={"PYTHONPATH": str(ROOT)})
    out, _ = proc.communicate(timeout=30)
    assert json.loads(out) == ALL_DENIED
    lines = _refusals(s, proc.pid)
    assert len(lines) == 1 and "not the DragShift app" in lines[0], lines


@gnome
def test_test_processes_are_let_in_here(cc_session, shell):
    """The test allowance: this pytest process and `-m tests.linux.*` helpers."""
    assert _in_process(cc_session) == ALL_OK
    assert _run(cc_session, [sys.executable, "-m", "tests.linux.caller"]) == ALL_OK


def _app_windows(shell, pid) -> set[int]:
    return {w["id"] for w in shell.list_windows() if w["pid"] == pid}


def _start_app(s, cmd, home: Path):
    log = (home / "app.err").open("w")
    proc = s.popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                   env={"QT_QPA_PLATFORM": "wayland", "PYTHONPATH": str(ROOT)})
    return proc, log


@gnome
def test_the_real_app_works(cc_session, shell, tmp_path):
    """`python -m dragshift` (the app itself, let in by the production rule, not the test
    allowance) gets the pointer: a Shift+drag on the desktop opens its menu."""
    s = cc_session
    proc, log = _start_app(s, [sys.executable, "-m", "dragshift", "--quiet"], tmp_path)
    try:
        time.sleep(4)                      # started, welcome window (fresh settings) shown
        assert proc.poll() is None, (tmp_path / "app.err").read_text()
        before = _app_windows(shell, proc.pid)
        inject.move(shell, 700, 500)
        with inject.hold_key(shell, "Shift_L"):
            inject.button_down(shell)
            time.sleep(0.2)                # the trigger (15 ms ticks) sees the press here
            # glide past the Linux drag threshold (#31), then hold still
            inject.glide(shell, 700 + LINUX_DRAG_THRESHOLD + 10, 500, dur=0.25)
            try:
                assert _wait(lambda: _app_windows(shell, proc.pid) - before, timeout=5), \
                    "no menu: " + (tmp_path / "app.err").read_text()
            finally:
                inject.button_up(shell)
        assert _refusals(s, proc.pid) == []
        assert "only answers the DragShift app" not in (tmp_path / "app.err").read_text()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        shell.release_all()


@gnome
def test_the_app_started_another_way_is_refused_and_says_so(cc_session, shell, tmp_path):
    """`python run.py` is the same app with a command line the check does not accept: no
    pointer, a refusal in the shell log and a hint on stderr."""
    s = cc_session
    proc, log = _start_app(s, [sys.executable, "run.py", "--quiet"], tmp_path)
    try:
        hint = "the GNOME Shell extension only answers the DragShift app"
        assert _wait(lambda: hint in (tmp_path / "app.err").read_text(), timeout=15), \
            (tmp_path / "app.err").read_text()
        assert _refusals(s, proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


@gnome
def test_get_pointer_latency(cc_session, shell):
    """GetPointer from a new connection: the first call pays for the check, every later
    one is a cache hit and costs what Version (no check) does."""
    conn = open_dbus_connection(bus=cc_session.bus_address)
    try:
        t = time.perf_counter()
        assert caller.call(conn, "GetPointer")[0] == "ok"
        first = (time.perf_counter() - t) * 1000
        times = {}
        for method in ("GetPointer", "Version", "GetPointer"):
            samples = []
            for _ in range(2000):
                t = time.perf_counter()
                caller.call(conn, method)
                samples.append((time.perf_counter() - t) * 1000)
            samples.sort()
            times[method] = samples
    finally:
        conn.close()
    p50 = {m: statistics.median(v) for m, v in times.items()}
    p99 = {m: v[len(v) * 99 // 100] for m, v in times.items()}
    print(f"\nGetPointer: first call (check) {first:.2f} ms; cached, 2000 calls: median "
          f"{p50['GetPointer']:.3f} ms, p99 {p99['GetPointer']:.3f} ms; Version (no check): "
          f"median {p50['Version']:.3f} ms, p99 {p99['Version']:.3f} ms")
    assert p50["GetPointer"] < 1.0
    assert p50["GetPointer"] < p50["Version"] + 0.15


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(cc_session, shell):
    assert _errors_about(cc_session.log_path.read_text(errors="replace")) == []


# ------------------------------------------------------------------ outside a session root
@gnome
def test_without_the_test_allowance(built_dir, monkeypatch, tmp_path):  # noqa: F811
    """A session whose root is not dragshift-session-*: the test processes are refused
    like everyone else, and the DragShift app's command line is still let in. Here that
    command line is the probe saved as dragshift/__main__.py: the check trusts a command
    line, which any process of the same user can fake (the documented limit)."""
    require_session_tools()
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, prefix=None, **kw):
        if prefix == "dragshift-session-":
            prefix = "dragshift-elsewhere-"
        return real_mkdtemp(*args, prefix=prefix, **kw)

    monkeypatch.setattr(session_mod.tempfile, "mkdtemp", mkdtemp)
    with Session(extensions=[built_dir]) as s:
        assert s.root.name.startswith("dragshift-elsewhere-")
        _ready(s)
        assert _wait(lambda: _in_process(s)["Version"] == "ok")
        assert _in_process(s) == ALL_DENIED
        assert _run(s, [sys.executable, "-m", "tests.linux.caller"]) == ALL_DENIED
        fake = _fake_app(tmp_path / "fake")
        assert _run(s, [sys.executable, "-m", "dragshift"], cwd=fake, pythonpath="") == ALL_OK
        assert _run(s, [sys.executable, str(fake / "dragshift" / "__main__.py")],
                    cwd=fake, pythonpath="") == ALL_OK
        r = _gdbus(s, "GetPointer")
        assert r.returncode != 0 and DENIED in r.stderr
        assert _errors_about(s.log_path.read_text(errors="replace")) == []
