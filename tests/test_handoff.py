"""The command-line and Open With hand-off on Linux (#50), without a desktop.

- Where the socket is: $XDG_RUNTIME_DIR/DragShift when that dir is private, else the old
  <tempdir>/DragShift-<user>; Windows and macOS keep their name and path.
- The client (handoff.try_handoff, before Qt) and the app's QLocalServer agree on it, and
  a socket left behind by a crash neither swallows a hand-off nor blocks the next start.
- `python -m dragshift a.jpg b.jpg` hands both files over without importing Qt.
- packaging/linux: the desktop entry (name = the app id, a MIME type for every file
  DragShift takes, valid) and the launcher (passes every argument through `-m dragshift`).

The session tests in tests/linux/test_handoff.py run the real app and Files."""
import configparser
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from dragshift import handoff, util

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "linux"
DESKTOP = PACKAGING / "io.github.eteen12.DragShift.desktop"
LAUNCHER = PACKAGING / "dragshift"

# The MIME type GIO gives each extension DragShift handles (Gio.content_type_guess on
# Ubuntu 24.04's shared-mime-info), which is what Files matches Open With entries by.
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".heic": "image/heif", ".heif": "image/heif",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".avif": "image/avif",
        ".bmp": "image/bmp", ".gif": "image/gif", ".ico": "image/vnd.microsoft.icon",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
        ".log": "text/x-log", ".json": "application/json", ".xml": "application/xml",
        ".html": "text/html", ".htm": "text/html"}


@pytest.fixture
def runtime(monkeypatch):
    """A private XDG_RUNTIME_DIR (0700), short enough for a unix socket path."""
    d = tempfile.mkdtemp(prefix="dsrt.", dir="/tmp")
    os.chmod(d, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeApp:
    """A listener on the hand-off socket that records each payload (no Qt)."""

    def __init__(self, path: str):
        self.path, self.payloads = path, []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.sock.settimeout(5)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                conn, _ = self.sock.accept()
                with conn:
                    data = b""
                    while chunk := conn.recv(65536):
                        data += chunk
                self.payloads.append(json.loads(data))
        except OSError:
            pass

    def wait(self, n=1, timeout=5.0):
        deadline = time.monotonic() + timeout
        while len(self.payloads) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.payloads

    def close(self):
        self.sock.close()
        os.unlink(self.path)


# ------------------------------------------------------------------ where the socket is
def test_linux_socket_is_in_the_private_runtime_dir(runtime):
    assert handoff.ipc_name() == str(runtime / "DragShift")
    assert handoff.socket_path() == str(runtime / "DragShift")


@pytest.mark.parametrize("value", ["", "relative/run", "/nonexistent/dragshift-run", "open"])
def test_without_a_private_runtime_dir_it_stays_in_the_temp_dir(monkeypatch, tmp_path, value):
    if value == "open":  # world-readable: not a runtime dir by the XDG spec
        value = str(tmp_path)
        os.chmod(value, 0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", value)
    monkeypatch.setenv("USER", "someone")
    monkeypatch.delenv("USERNAME", raising=False)
    assert handoff.ipc_name() == "DragShift-someone"
    assert handoff.socket_path() == os.path.join(tempfile.gettempdir(), "DragShift-someone")


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_windows_and_macos_keep_their_name(monkeypatch, runtime, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("USER", "someone")
    monkeypatch.delenv("USERNAME", raising=False)
    assert handoff.ipc_name() == "DragShift-someone"
    if platform == "darwin":
        assert handoff.socket_path() == os.path.join(tempfile.gettempdir(), "DragShift-someone")


def test_the_session_env_of_a_child_decides(runtime, tmp_path):
    """ipc_name(env) answers for another environment (the isolated session's tests)."""
    other = tmp_path / "run"
    other.mkdir(mode=0o700)
    assert handoff.socket_path({"XDG_RUNTIME_DIR": str(other)}) == str(other / "DragShift")


# ------------------------------------------------------------------ client and server agree
def test_the_apps_server_and_the_client_meet(runtime, tmp_path):
    """QLocalServer.listen(ipc_name()) (what DragShiftApp does) creates socket_path(), and
    both try_handoff (before Qt) and send_to_running (Qt) reach it."""
    _qapp()
    from PySide6.QtNetwork import QLocalServer
    from dragshift import app
    name = handoff.ipc_name()
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name), server.errorString()
    try:
        assert server.fullServerName() == handoff.socket_path()
        assert oct(os.stat(runtime).st_mode & 0o777) == "0o700"
        pic = tmp_path / "a b.jpg"
        pic.write_bytes(b"x")

        def received():
            assert server.waitForNewConnection(3000)
            sock = server.nextPendingConnection()
            data = b""
            while sock.waitForReadyRead(500) or sock.bytesAvailable():
                data += bytes(sock.readAll())
            return json.loads(data)

        assert handoff.try_handoff(["dragshift", str(pic)])
        assert received() == {"files": [str(pic)]}
        old = app.IPC_NAME
        app.IPC_NAME = name   # app.py read ipc_name() at import, before this test's env
        try:
            assert app.send_to_running({"show": True})
        finally:
            app.IPC_NAME = old
        assert received() == {"show": True}
    finally:
        server.close()
    assert not os.path.exists(handoff.socket_path())


def test_a_socket_left_by_a_crash(runtime):
    """Nothing listens on a stale socket: the hand-off fails at once (so the app starts),
    and the app's removeServer + listen take the name over."""
    path = handoff.socket_path()
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(path)
    dead.close()                       # the file stays, like after a SIGKILL
    assert os.path.exists(path)
    t0 = time.monotonic()
    assert not handoff.try_handoff(["dragshift", "--quiet"])
    assert time.monotonic() - t0 < 0.2
    _qapp()
    from PySide6.QtNetwork import QLocalServer
    QLocalServer.removeServer(handoff.ipc_name())
    server = QLocalServer()
    assert server.listen(handoff.ipc_name()), server.errorString()
    server.close()


def test_payloads(runtime, tmp_path, monkeypatch):
    """Relative paths resolve against the caller's cwd, missing ones are dropped, no files
    means "show" (unless --quiet), and nothing listening means no hand-off."""
    assert not handoff.try_handoff(["dragshift", "x.jpg"])
    fake = FakeApp(handoff.socket_path())
    try:
        (tmp_path / "rel.jpg").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        assert handoff.try_handoff(["dragshift", "rel.jpg", "missing.jpg", "--quiet"])
        assert handoff.try_handoff(["dragshift", "missing.jpg"])
        assert handoff.try_handoff(["dragshift", "--quiet"])
        assert fake.wait(3) == [{"files": [str(tmp_path / "rel.jpg")]}, {"show": True},
                                {"show": False}]
        assert not handoff.try_handoff(["dragshift", "--selftest"])
    finally:
        fake.close()


def test_python_m_dragshift_hands_over_before_importing_qt(runtime, tmp_path):
    """The real entry point, as the launcher runs it. Qt is given a platform that does not
    exist, so if the hand-off failed the app could not start (and the test fails)."""
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    fake = FakeApp(handoff.socket_path())
    try:
        env = dict(os.environ, PYTHONPATH=str(ROOT), QT_QPA_PLATFORM="no-such-platform")
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-X", "importtime", "-m", "dragshift", "a.jpg",
                            str(b)], cwd=tmp_path, env=env, capture_output=True, text=True,
                           timeout=20)
        took = time.monotonic() - t0
        assert r.returncode == 0, r.stderr[-2000:]
        assert fake.wait(1) == [{"files": [str(a), str(b)]}]
        imported = [line.rsplit("|", 1)[-1].strip() for line in r.stderr.splitlines()
                    if line.startswith("import time:")]
        assert "dragshift.handoff" in imported
        assert not [m for m in imported if m.startswith("PySide6")]
        print(f"\npython -m dragshift a.jpg b.jpg: handed over and exited in {took * 1000:.0f} ms")
        assert took < 1.0
    finally:
        fake.close()


# ------------------------------------------------------------------ packaging/linux
def _entry() -> configparser.SectionProxy:
    cp = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    cp.optionxform = str
    cp.read(DESKTOP, encoding="utf-8")
    return cp["Desktop Entry"]


def test_desktop_entry_is_named_after_the_app_id():
    """#11: Wayland windows carry the app id, and GNOME links them to <app id>.desktop."""
    from dragshift import app
    assert DESKTOP.name == app.LINUX_APP_ID + ".desktop"
    e = _entry()
    assert e["Icon"] == app.LINUX_APP_ID
    assert e["Exec"] == "dragshift %F"          # several files → one launch → one menu
    assert e["Type"] == "Application" and e["NoDisplay"] == "false"
    assert e["Terminal"] == "false"


def test_desktop_entry_lists_every_file_type_dragshift_takes():
    exts = util.IMAGE_EXTS | util.PDF_EXTS | util.DOCX_EXTS | util.TEXT_EXTS
    assert set(MIME) == exts
    listed = _entry()["MimeType"].rstrip(";").split(";")
    assert len(listed) == len(set(listed))
    assert sorted(set(MIME.values()) - set(listed)) == []


@pytest.mark.skipif(not shutil.which("desktop-file-validate"), reason="desktop-file-utils missing")
def test_desktop_entry_is_valid():
    r = subprocess.run(["desktop-file-validate", str(DESKTOP)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout + r.stderr == "", r.stdout + r.stderr


def test_launcher_runs_the_venv_python_with_every_argument(tmp_path):
    """Same form as #93's caller check expects; arguments with spaces survive; the caller's
    cwd stays (relative paths); the source dir goes on PYTHONPATH when it exists."""
    text = LAUNCHER.read_text()
    assert text.startswith("#!/bin/sh\n") and os.access(LAUNCHER, os.X_OK)
    assert text.rstrip().splitlines()[-1] == 'exec "$VENV/bin/python" -m dragshift "$@"'
    venv, src = tmp_path / "venv", tmp_path / "src"
    (venv / "bin").mkdir(parents=True)
    (src / "dragshift").mkdir(parents=True)
    record = tmp_path / "argv.json"
    fake = venv / "bin" / "python"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"json.dump([sys.argv[1:], os.getcwd(), os.environ.get('PYTHONPATH')], "
                    f"open({str(record)!r}, 'w'))\n")
    fake.chmod(0o755)
    cwd = tmp_path / "here"
    cwd.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "DRAGSHIFT_VENV": str(venv), "DRAGSHIFT_SRC": str(src)}
    r = subprocess.run([str(LAUNCHER), "a b.jpg", "c.pdf"], cwd=cwd, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(record.read_text()) == [["-m", "dragshift", "a b.jpg", "c.pdf"],
                                              str(cwd), str(src)]
    # the installer's default layout under XDG_DATA_HOME
    data = tmp_path / "data" / "dragshift"
    (data / "venv" / "bin").mkdir(parents=True)
    shutil.copy(fake, data / "venv" / "bin" / "python")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "XDG_DATA_HOME": str(tmp_path / "data")}
    r = subprocess.run([str(LAUNCHER)], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(record.read_text()) == [["-m", "dragshift"], str(cwd), None]
