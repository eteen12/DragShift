"""Escape while the menu is open (#24): the extension's WatchEscape/EscapePressed, its
client platform_linux.watch_escape(), and what a real file drag from Files or the
desktop does when Escape is pressed.

Measured in the isolated session (GNOME Shell 46): during a drag Mutter's drag grab
takes Escape itself and cancels the drag, so EscapePressed never arrives. What the
drop target sees instead is a dragLeave a few ms later while the button is still down,
and no drop when the button goes up. #32 closes the menu on exactly that. Outside a
drag (the menu's click mode) the grab reports Escape and the focused window never gets
the key press, so nothing fires twice.

Session tests load the extension only inside the isolated session from
tests/linux/session.py and inject input with tests/linux/inject.py.
"""
import io
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from dragshift import platform_linux as lx
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.probe_window import ProbeWindow, qt_platform
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (ROOT, SRC, _errors_about, _has_owner, _state,  # noqa: F401
                                        _wait, UUID, built_dir, built_zip)
from tests.linux.test_pointer import _reset_client

gnome = pytest.mark.gnome

PROBE_RECT = (900, 200, 520, 440)       # between the Files window and the desktop icons
CANCEL_WITHIN = 0.3                     # s from Escape to dragLeave (#32 hides in 300 ms)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (220, 30, 30)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _evidence(path: Path, name: str) -> None:
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        shutil.copy(path, Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"]) / name)


# ------------------------------------------------------------------ no shell
def test_dbus_xml_declares_watch_escape():
    iface = ET.parse(SRC / "dbus.xml").getroot().find("interface")
    method = iface.find("method[@name='WatchEscape']")
    assert [(a.get("name"), a.get("type"), a.get("direction")) for a in method.findall("arg")] \
        == [("on", "b", "in"), ("watching", "b", "out")]
    sig = iface.find("signal[@name='EscapePressed']")
    assert sig is not None and sig.findall("arg") == []


def test_watch_escape_without_a_session_bus(tmp_path):
    code = ("import time; from dragshift import platform_linux as p; t = time.monotonic(); "
            "v = p.watch_escape(True, print); print(v, p.watch_escape(False), "
            "round((time.monotonic() - t) * 1000))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=30, env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                                        "DBUS_SESSION_BUS_ADDRESS":
                                            f"unix:path={tmp_path}/none"})
    assert r.returncode == 0, r.stderr
    on, off, ms = r.stdout.split()
    assert (on, off) == ("False", "False") and int(ms) < 500


@pytest.mark.skipif(not shutil.which("dbus-run-session"), reason="needs dbus-run-session")
def test_watch_escape_without_the_extension(tmp_path):
    """A bus without the extension: False at once, nothing left open."""
    code = ("import threading, time; from dragshift import platform_linux as p; "
            "t = time.monotonic(); v = p.watch_escape(True, print); "
            "print(v, p._esc is None, round((time.monotonic() - t) * 1000), "
            "threading.active_count())")
    r = subprocess.run(["dbus-run-session", "--", sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                            "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    v, closed, ms, threads = r.stdout.split()
    assert (v, closed, threads) == ("False", "True", "1") and int(ms) < 500


# ------------------------------------------------------------------ in the session
@pytest.fixture(scope="module")
def esc_session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(esc_session):
    with ShellHelper(esc_session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(esc_session)), _state(esc_session)
        yield helper


@pytest.fixture
def client(esc_session, shell, monkeypatch):
    """platform_linux in this process on the isolated session's bus; every Escape the
    extension reports is appended to client.hits (time.monotonic())."""
    _reset_client()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", esc_session.bus_address)
    assert _wait(lambda: lx._call("Version") is not None)
    lx.hits = []
    try:
        yield lx
    finally:
        lx.watch_escape(False)
        del lx.hits
        shell.release_all()
        _reset_client()


def _record(client):
    return lambda: client.hits.append(time.monotonic())


@pytest.fixture
def files(esc_session, shell):
    f = Files(esc_session, shell)
    yield f
    f.close()


def _probe(session, shell, platform="wayland"):
    if platform == "xcb" and qt_platform(session) != "xcb":
        pytest.skip("Qt xcb plugin does not load here (libxcb-cursor0 missing)")
    probe = ProbeWindow(session, shell, platform=platform)
    try:
        probe.move(*PROBE_RECT)
    except BaseException:
        probe.close()
        raise
    return probe


def _watcher_process(session):
    """Another process that holds the Escape watch until it is killed."""
    code = ("import sys, time; from dragshift import platform_linux as p; "
            "print(p.watch_escape(True, lambda: None), flush=True); time.sleep(600)")
    proc = session.popen([sys.executable, "-c", code], env={"PYTHONPATH": str(ROOT)},
                         stdout=subprocess.PIPE, text=True)
    return proc, proc.stdout.readline().strip()


def _escape_reaches(probe, shell) -> bool:
    mark = probe.mark()
    inject.press(shell, "Escape")
    try:
        probe.wait_for("keyPress", lambda r: r["key"] == "Key_Escape", after=mark, timeout=1)
        return True
    except AssertionError:
        return False


@gnome
@pytest.mark.parametrize("platform", ["wayland", "xcb"])
@pytest.mark.parametrize("source", ["nautilus", "desktop"])
def test_escape_during_a_drag_cancels_it(esc_session, shell, client, files, source,
                                         platform):
    """Acceptance: a real Shift+drag of Photo.jpg into the probe, Escape with the button
    still held → the probe logs dragLeave within CANCEL_WITHIN while pointer_state() still
    reports the button down; releasing it drops nothing and the file stays."""
    s = esc_session
    if source == "nautilus":
        folder = files.make_folder(f"pics-{secrets.token_hex(3)}", {"Photo.jpg": _jpeg()})
        files.open_folder(folder)
        paths = []
    else:
        paths = files.put_on_desktop({"Photo.jpg": _jpeg()})
        folder = files.desktop_dir()
    try:
        start = files.icon_center("Photo.jpg", source)
        before = sorted(p.name for p in folder.iterdir())
        with _probe(s, shell, platform) as probe:
            assert client.watch_escape(True, _record(client))
            mark = probe.mark()
            inject.drag_to(shell, *probe.center(), start=start, hold="Shift_L", dur=0.8,
                           release=False)
            probe.wait_for("dragEnter", after=mark, timeout=10)
            shot = shell.screenshot(s.root / "tmp" / f"escape-{source}-{platform}-before.png")
            mark = probe.mark()
            pressed = time.monotonic()
            inject.press(shell, "Escape")
            leave = probe.wait_for("dragLeave", after=mark, timeout=2)
            latency = leave["t"] - pressed
            x, y, left, mask = client.pointer_state()
            assert left and mask & lx.SHIFT_MASK, "the button must still be down"
            assert latency < CANCEL_WITHIN, latency
            time.sleep(0.3)
            after = shell.screenshot(s.root / "tmp" / f"escape-{source}-{platform}-after.png")
            mark = probe.mark()
            inject.button_up(shell)
            shell.release_all()
            time.sleep(1.0)                   # a drop would arrive well within this
            assert [r for r in probe.events()[mark:] if r["type"] in ("drop", "dragEnter")] == []
            print(f"\n{source}/{platform}: dragLeave {latency * 1000:.1f} ms after Escape, "
                  f"button still down (mask {mask:#x}); EscapePressed during the drag: "
                  f"{len(client.hits)}")
        assert sorted(p.name for p in folder.iterdir()) == before
        assert (folder / "Photo.jpg").read_bytes() == _jpeg()
        _evidence(shot, shot.name)
        _evidence(after, after.name)
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


@gnome
def test_click_mode_escape_fires_once_and_the_window_sees_no_press(esc_session, shell,
                                                                    client):
    """Edge case: Escape without a drag (click mode). The grab reports it once and the
    focused window gets no key press, so Qt's keyPressEvent can't fire as well; after
    watch_escape(False) Escape reaches the window again."""
    with _probe(esc_session, shell) as probe:
        probe.focus()
        t = time.perf_counter()
        assert client.watch_escape(True, _record(client))
        on_ms = (time.perf_counter() - t) * 1000
        assert client.watch_escape(True, _record(client))     # again: same grab
        mark = probe.mark()
        pressed = time.monotonic()
        inject.press(shell, "Escape")
        assert _wait(lambda: client.hits, timeout=1)
        time.sleep(0.3)
        assert len(client.hits) == 1 and client.hits[0] - pressed < CANCEL_WITHIN
        assert [r for r in probe.events()[mark:] if r["type"] == "keyPress"] == []
        t = time.perf_counter()
        assert client.watch_escape(False) is False
        off_ms = (time.perf_counter() - t) * 1000
        assert _escape_reaches(probe, shell)
        assert len(client.hits) == 1
        print(f"\nclick mode: EscapePressed {(client.hits[0] - pressed) * 1000:.1f} ms "
              "after Escape; no keyPress in the window while watching; "
              f"watch_escape(True) {on_ms:.1f} ms, watch_escape(False) {off_ms:.1f} ms")
        assert on_ms < 100 and off_ms < 100


@gnome
def test_grab_ends_with_the_callers_connection(esc_session, shell, client):
    """Escape is never left grabbed: a second caller is refused while another holds it,
    and when that process dies (no WatchEscape(false)) the grab goes with it."""
    with _probe(esc_session, shell) as probe:
        probe.focus()
        proc, ok = _watcher_process(esc_session)
        try:
            assert ok == "True"
            assert not _escape_reaches(probe, shell)
            assert client.watch_escape(True, _record(client)) is False
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            proc.stdout.close()
        assert _wait(lambda: _escape_reaches(probe, shell), timeout=3)
        assert client.watch_escape(True, _record(client))
        inject.press(shell, "Escape")
        assert _wait(lambda: client.hits, timeout=1)


@gnome
def test_disabling_the_extension_releases_escape(esc_session, shell, client):
    s = esc_session
    with _probe(s, shell) as probe:
        probe.focus()
        assert client.watch_escape(True, _record(client))
        assert not _escape_reaches(probe, shell)
        try:
            assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
            assert _wait(lambda: not _has_owner(s))
            # The shell re-enables the extensions after this one (the input helper
            # among them), so the first key right after the disable can get lost.
            assert _wait(lambda: _escape_reaches(probe, shell), timeout=3)
            assert client.watch_escape(False) is False        # no error, nothing to undo
            assert client.watch_escape(True, _record(client)) is False
        finally:
            assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
        assert _wait(lambda: _has_owner(s))
        assert _wait(lambda: client.watch_escape(True, _record(client)))
        assert not _escape_reaches(probe, shell)
        assert _wait(lambda: client.hits, timeout=1)


# An extension that already holds Escape as a global accelerator.
HOLDER_UUID = "escape-holder@dragshift.test"
HOLDER_JS = """
import Meta from 'gi://Meta';

export default class Holder {
    enable() {
        this._action = global.display.grab_accelerator('Escape', Meta.KeyBindingFlags.NONE);
    }

    disable() {
        global.display.ungrab_accelerator(this._action);
    }
}
"""


@gnome
def test_escape_held_by_another_extension(built_dir, tmp_path, monkeypatch):  # noqa: F811
    """Edge case: another extension holds Escape → WatchEscape returns false and the
    shell log says why; the app falls back to watching the drag end."""
    require_session_tools()
    holder = tmp_path / HOLDER_UUID
    holder.mkdir()
    (holder / "metadata.json").write_text(
        '{"uuid": "%s", "name": "Holder", "description": "test", "shell-version": ["46"]}'
        % HOLDER_UUID)
    (holder / "extension.js").write_text(HOLDER_JS)
    with Session(extensions=[holder, built_dir]) as s:
        assert _wait(lambda: _state(s, HOLDER_UUID) == "ACTIVE")
        assert _wait(lambda: _has_owner(s))
        _reset_client()
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", s.bus_address)
        try:
            assert lx.watch_escape(True, lambda: None) is False
            assert lx._esc is None
        finally:
            lx.watch_escape(False)
            _reset_client()
        log = s.log_path.read_text(errors="replace")
        assert "DragShift: could not grab Escape" in log
        assert _errors_about(log) == []


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(esc_session, shell):
    assert _errors_about(esc_session.log_path.read_text(errors="replace")) == []


def test_signal_calls_the_current_callback_and_survives_errors(monkeypatch, capsys):
    """Each EscapePressed calls the callback in place at that moment; an exception in it
    is printed instead of stopping the receiver thread."""
    seen = []
    monkeypatch.setattr(lx, "_esc_cb", lambda: seen.append(1))
    lx._OnSignal().put_nowait(None)
    monkeypatch.setattr(lx, "_esc_cb", lambda: 1 / 0)
    lx._OnSignal().put_nowait(None)
    monkeypatch.setattr(lx, "_esc_cb", None)
    lx._OnSignal().put_nowait(None)
    assert seen == [1] and "ZeroDivisionError" in capsys.readouterr().err
