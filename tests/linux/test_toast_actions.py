"""Toast actions inside the isolated GNOME session (#43): Show in folder, Undo, Open.

A driver process in the session converts a PNG to JPG in the session's HOME, shows the
result card and clicks one of its buttons. Files, the Trash and the default-app lookup are
all the session's own; nothing here can reach the real desktop."""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux only", allow_module_level=True)
pytest.importorskip("tests.linux.session", reason="needs the #14 session launcher")

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]

DRIVER = r"""
import json, sys
from pathlib import Path
from PIL import Image
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication([])
from dragshift import convert, report, toasts

def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

button, work = sys.argv[1], Path(sys.argv[2])
work.mkdir(parents=True)
src = work / "photo.png"
Image.new("RGB", (64, 48), (200, 80, 40)).save(src)
outs = convert.image_to(src, "JPG")
card = toasts.ToastStack().new()
card.done(report.summarize("JPG", [src], outs))
spin(500)                                   # painted: card.buttons now holds the rects
rect = dict(card.buttons)[button]
QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, rect.center().toPoint())
spin(1500)                                  # let the D-Bus reply / xdg-open happen
print(json.dumps({"outs": [str(o) for o in outs], "buttons": [n for n, _ in card.buttons],
                  "state": card.state, "title": card.title, "detail": card.detail}))
"""


def _drive(session, button: str, name: str) -> dict:
    work = Path(session.env()["HOME"]) / name
    r = session.run([sys.executable, "-c", DRIVER, button, str(work)], cwd=ROOT,
                    env={"QT_QPA_PLATFORM": "wayland", "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.splitlines()[-1])


def _wait_for(fn, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.25)
    return fn()


def _files_windows(session) -> str:
    """Files' own list of its windows and the folders they show (empty when not running)."""
    r = session.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                     "--object-path", "/org/freedesktop/FileManager1", "--method",
                     "org.freedesktop.DBus.Properties.Get", "org.freedesktop.FileManager1",
                     "OpenWindowsWithLocations"])
    return r.stdout if r.returncode == 0 else ""


def test_show_in_folder_opens_files_on_the_output(gnome_session):
    s = gnome_session
    monitor = s.popen(["dbus-monitor", "--session",
                       "type='method_call',interface='org.freedesktop.FileManager1',"
                       "member='ShowItems'"], stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(0.5)
        res = _drive(s, "Show", "show")
        out = Path(res["outs"][0])
        assert res["buttons"] == ["Open", "Show", "Undo"]
        windows = _wait_for(lambda: out.parent.as_uri() in _files_windows(s) and _files_windows(s))
        assert out.parent.as_uri() in windows, windows
    finally:
        monitor.terminate()
        seen = monitor.communicate(timeout=10)[0]
    assert "member=ShowItems" in seen
    assert f'string "{out.as_uri()}"' in seen


def test_undo_moves_the_output_to_the_sessions_trash(gnome_session):
    s = gnome_session
    res = _drive(s, "Undo", "undo")
    out = Path(res["outs"][0])
    trash = Path(s.env()["XDG_DATA_HOME"]) / "Trash"
    assert (res["state"], res["title"], res["detail"]) == \
        ("info", "Undone", "Moved 1 file to the Trash")
    assert not out.exists()
    assert (trash / "files" / out.name).is_file()
    info = (trash / "info" / (out.name + ".trashinfo")).read_text()
    assert re.search(r"^Path=.*photo\.jpg$", info, re.M), info
    assert (out.parent / "photo.png").is_file()                      # the source stays


def test_open_launches_the_default_app(gnome_session):
    s = gnome_session
    env = s.env()
    missing = [t for t in ("xdg-open", "xdg-mime") if not shutil.which(t, path=env.get("PATH"))]
    if missing:   # Open itself falls back to Qt then, which this test doesn't cover
        pytest.skip("needs xdg-utils, missing: " + ", ".join(missing))
    marker = Path(env["HOME"]) / "opened.txt"
    script = Path(env["HOME"]) / "open-marker.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{marker}"\n')
    script.chmod(0o755)
    apps = Path(env["XDG_DATA_HOME"]) / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "dragshift-test-open.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=DragShift test opener\n"
        f"Exec={script} %f\nMimeType=image/jpeg;\nNoDisplay=true\n")
    r = s.run(["xdg-mime", "default", "dragshift-test-open.desktop", "image/jpeg"])
    assert r.returncode == 0, r.stderr
    res = _drive(s, "Open", "open")
    assert _wait_for(marker.exists), "the test handler never ran"
    assert marker.read_text() == res["outs"][0]
