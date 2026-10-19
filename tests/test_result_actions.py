"""Toast result actions: Open, Show in folder, Undo (#43).

Nothing here reaches the real desktop: tests/conftest.py points the session bus at a socket
that does not exist, Undo runs with a temporary HOME and XDG_DATA_HOME (so the Trash is a
throwaway one), and the D-Bus tests run in a subprocess on a private dbus-daemon that has no
service files, so no file manager can be started."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dragshift.report import Result

ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux only")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def card(qapp):
    from dragshift import toasts
    c = toasts.ToastStack().new()
    yield c
    c.life.stop()
    c.deleteLater()


@pytest.fixture
def temp_trash(monkeypatch, tmp_path):
    """HOME and XDG_DATA_HOME inside tmp_path: Qt's moveToTrash then uses tmp_path's Trash."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "home").mkdir()
    (tmp_path / "data").mkdir()         # Qt only creates the Trash folder inside an existing one
    return tmp_path / "data" / "Trash"


def _outs(folder: Path, *names: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"x")
    return [folder / n for n in names]


# ---------------------------------------------------------------- Undo wording
@pytest.mark.parametrize("platform, word", [("linux", "Trash"), ("darwin", "Trash"),
                                            ("win32", "Recycle Bin")])
def test_undo_names_the_platform_bin(card, monkeypatch, tmp_path, platform, word):
    from dragshift import toasts
    monkeypatch.setattr(toasts.sys, "platform", platform)
    monkeypatch.setattr(toasts, "trash", lambda outs: len(outs))
    card.done(Result("PNG → JPG", "", _outs(tmp_path, "a.jpg")))
    card._act("Undo")
    assert (card.state, card.title, card.detail) == ("info", "Undone", f"Moved 1 file to the {word}")
    card.done(Result("Split", "", _outs(tmp_path, "p1.pdf", "p2.pdf")))
    card._act("Undo")
    assert card.detail == f"Moved 2 files to the {word}"


@linux_only
def test_undo_moves_outputs_to_the_freedesktop_trash(card, temp_trash, tmp_path):
    outs = _outs(tmp_path / "work" / "x images", "page 1 image 1.png", "page 1 image 2.png")
    card.done(Result("Extracted 2 images", "", outs))
    card._act("Undo")
    assert card.detail == "Moved 2 files to the Trash"
    assert sorted(p.name for p in (temp_trash / "files").iterdir()) == \
        ["page 1 image 1.png", "page 1 image 2.png"]
    assert (temp_trash / "info" / "page 1 image 1.png.trashinfo").is_file()
    assert not (tmp_path / "work" / "x images").exists()      # emptied output folder removed
    assert (tmp_path / "work").is_dir()


@linux_only
def test_undo_deletes_when_no_trash_takes_the_file(card, temp_trash, tmp_path, monkeypatch):
    """The documented fallback (e.g. another filesystem without a usable .Trash-$UID)."""
    from dragshift import toasts
    monkeypatch.setattr(toasts.QFile, "moveToTrash", staticmethod(lambda p: False))
    out, = _outs(tmp_path / "work", "a Compressed.jpg")
    card.done(Result("Compressed", "", [out]))
    card._act("Undo")
    assert not out.exists() and not temp_trash.exists()
    assert card.detail == "Moved 1 file to the Trash"


# ---------------------------------------------------------------- Open / Show / card body
def test_open_and_show_use_the_first_output(card, monkeypatch, tmp_path):
    from dragshift import toasts
    calls = []
    monkeypatch.setattr(toasts, "open_file", lambda p: calls.append(("open", p)))
    monkeypatch.setattr(toasts, "reveal", lambda p: calls.append(("reveal", p)))
    outs = _outs(tmp_path, "a page 1.png", "a page 2.png")
    for name in ("Open", "Show"):
        card.done(Result("PDF → PNG", "", outs))
        card._act(name)
    assert calls == [("open", outs[0]), ("reveal", outs[0])]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_open_uses_qt_on_windows_and_macos(qapp, monkeypatch, tmp_path, platform):
    from dragshift import toasts
    urls, spawned = [], []
    monkeypatch.setattr(toasts.sys, "platform", platform)
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    monkeypatch.setattr(toasts.QDesktopServices, "openUrl", staticmethod(urls.append))
    toasts.open_file(tmp_path / "a b.png")
    assert [Path(u.toLocalFile()) for u in urls] == [tmp_path / "a b.png"] and spawned == []


@linux_only
def test_open_starts_the_default_app_with_xdg_open(qapp, monkeypatch, tmp_path):
    """Not Qt: Qt goes through the desktop portal, which shows an "Open with" chooser."""
    from dragshift import toasts
    urls, spawned = [], []
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    monkeypatch.setattr(toasts.QDesktopServices, "openUrl", staticmethod(urls.append))
    toasts.open_file(tmp_path / "a b.png")
    assert spawned == [["xdg-open", str(tmp_path / "a b.png")]] and urls == []


@linux_only
def test_open_falls_back_to_qt_without_xdg_open(qapp, monkeypatch, tmp_path):
    from dragshift import toasts
    urls = []

    def missing(cmd, *a, **k):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(toasts.subprocess, "Popen", missing)
    monkeypatch.setattr(toasts.QDesktopServices, "openUrl", staticmethod(urls.append))
    toasts.open_file(tmp_path / "a.png")
    assert [Path(u.toLocalFile()) for u in urls] == [tmp_path / "a.png"]


# ---------------------------------------------------------------- reveal over D-Bus
REVEAL = r"""
import json, subprocess, sys
spawned = []
subprocess.Popen = lambda cmd, *a, **k: spawned.append(cmd)
from PySide6.QtCore import ClassInfo, QCoreApplication, QEventLoop, QObject, QTimer, Slot
from PySide6.QtDBus import QDBusConnection
app = QCoreApplication([])
got = []

@ClassInfo({"D-Bus Interface": "org.freedesktop.FileManager1"})
class FileManager(QObject):
    @Slot("QStringList", str)
    def ShowItems(self, uris, startup_id):
        got.append([list(uris), startup_id])

if sys.argv[2] == "files":          # a stand-in for GNOME Files on the private bus
    fm, bus = FileManager(), QDBusConnection.connectToBus(QDBusConnection.SessionBus, "fm")
    assert bus.registerService("org.freedesktop.FileManager1")
    assert bus.registerObject("/org/freedesktop/FileManager1", fm, QDBusConnection.ExportAllSlots)

from dragshift import toasts
toasts.reveal(sys.argv[1])
loop = QEventLoop()
QTimer.singleShot(3000, loop.quit)
poll = QTimer(interval=20, timeout=lambda: (got or spawned) and loop.quit())
poll.start()
loop.exec()
print(json.dumps({"got": got, "spawned": spawned}))
"""


@pytest.fixture
def private_bus(tmp_path):
    """A dbus-daemon of our own with no service directories (nothing can be activated)."""
    if not (LINUX and shutil.which("dbus-daemon")):
        pytest.skip("needs dbus-daemon")
    conf = tmp_path / "bus.conf"
    conf.write_text(
        '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"'
        ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">'
        f'<busconfig><type>session</type><listen>unix:path={tmp_path}/bus</listen>'
        '<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*" eavesdrop="true"/>'
        '<allow eavesdrop="true"/>'
        '<allow own="*"/></policy></busconfig>')
    d = subprocess.Popen(["dbus-daemon", "--config-file", str(conf), "--nofork",
                          "--print-address"], stdout=subprocess.PIPE, text=True)
    address = d.stdout.readline().strip()
    yield address
    d.terminate()
    d.wait(5)


def _reveal(private_bus, tmp_path, path, mode):
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(DBUS_SESSION_BUS_ADDRESS=private_bus, QT_QPA_PLATFORM="offscreen",
               HOME=str(tmp_path), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", REVEAL, str(path), mode], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.splitlines()[-1])


@linux_only
def test_reveal_asks_files_to_select_the_output(private_bus, tmp_path):
    out = tmp_path / "out dir" / "photo ü.png"
    res = _reveal(private_bus, tmp_path, out, "files")
    assert res == {"got": [[[out.as_uri()], ""]], "spawned": []}
    assert out.as_uri().endswith("/out%20dir/photo%20%C3%BC.png")


@linux_only
def test_reveal_opens_the_folder_when_files_is_missing(private_bus, tmp_path):
    out = tmp_path / "out dir" / "photo.png"
    res = _reveal(private_bus, tmp_path, out, "none")
    assert res == {"got": [], "spawned": [["xdg-open", str(out.parent)]]}


@linux_only
def test_reveal_without_a_session_bus_opens_the_folder(qapp, monkeypatch, tmp_path):
    """The test run has no session bus (conftest), so platform_linux declines and
    toasts.reveal runs its own xdg-open fallback."""
    from dragshift import platform_linux, toasts
    spawned = []
    monkeypatch.setattr(toasts.subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    assert platform_linux.reveal(tmp_path / "out.png") is False
    toasts.reveal(tmp_path / "out.png")
    assert spawned == [["xdg-open", str(tmp_path)]]
