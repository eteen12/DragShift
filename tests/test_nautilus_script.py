"""The Files script packaging/linux/nautilus-scripts/DragShift… (#51), without a desktop.

The script is run the way Nautilus 46 and DING run it (selected files as arguments, the
NAUTILUS_SCRIPT_* variables set, the folder as working directory) against a fake
~/.local/bin/dragshift that records its argv, under every POSIX shell on this machine:
- it hands over absolute paths, exactly, for names with spaces, unicode, a newline, a
  leading "-", a backslash or a "%";
- without arguments it decodes NAUTILUS_SCRIPT_SELECTED_URIS, then reads
  NAUTILUS_SCRIPT_SELECTED_FILE_PATHS;
- with nothing selected (the folder's background) it runs the launcher with no files;
- it finds the launcher in ~/.local/bin, on PATH, or a checkout in DRAGSHIFT_SRC.
One test runs the whole chain: script → the real launcher → `python -m dragshift` → the
hand-off socket. tests/linux/test_nautilus_script.py drives it from Files' menu."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_handoff import LAUNCHER, ROOT, FakeApp, runtime  # noqa: F401

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

SCRIPT = ROOT / "packaging" / "linux" / "nautilus-scripts" / "DragShift…"
SHELLS = [s for s in ("sh", "dash", "bash", "busybox") if shutil.which(s)]
NAMES = ["Photo.jpg", "two words.jpg", "é ü 写真.jpg", "new\nline.jpg", "-dash.jpg",
         "back\\slash.jpg", "100%25 sure.png", "trailing newline\n"]


def _uri(path: Path) -> str:
    return path.as_uri()


def _argv(shell: str) -> list[str]:
    if shell == "sh":
        return [str(SCRIPT)]            # the shebang, as Nautilus runs it
    if shell == "busybox":
        return ["busybox", "sh", str(SCRIPT)]
    return [shell, str(SCRIPT)]


class Home:
    """A HOME with a fake launcher that records argv and working directory."""

    def __init__(self, tmp_path: Path, launcher: bool = True):
        self.home = tmp_path / "home"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.folder = tmp_path / "folder"
        self.folder.mkdir()
        self.record = tmp_path / "argv.json"
        if launcher:
            self.fake(self.bin / "dragshift")

    def fake(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                        f"json.dump([sys.argv[1:], os.getcwd(), {str(path)!r}], "
                        f"open({str(self.record)!r}, 'w'))\n")
        path.chmod(0o755)
        return path

    def files(self, names=NAMES) -> list[Path]:
        out = []
        for n in names:
            (self.folder / n).write_bytes(b"x")
            out.append(self.folder / n)
        return out

    def run(self, shell: str, *args: str, path: str = "/usr/bin:/bin", **env):
        e = {"HOME": str(self.home), "PATH": path,
             "NAUTILUS_SCRIPT_CURRENT_URI": _uri(self.folder), **env}
        self.record.unlink(missing_ok=True)
        r = subprocess.run([*_argv(shell), *args], cwd=self.folder, env=e,
                           capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr
        argv, cwd, launcher = json.loads(self.record.read_text())
        assert cwd == str(self.home)            # the folder is not kept busy
        return argv, launcher


def _nautilus_env(paths) -> dict:
    """The variables as Nautilus 46.4 sets them: every entry ends in a newline."""
    return {"NAUTILUS_SCRIPT_SELECTED_FILE_PATHS": "".join(f"{p}\n" for p in paths),
            "NAUTILUS_SCRIPT_SELECTED_URIS": "".join(f"{_uri(p)}\n" for p in paths)}


def test_the_script_is_an_executable_posix_sh_script():
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n") and os.access(SCRIPT, os.X_OK)
    if shutil.which("checkbashisms"):
        r = subprocess.run(["checkbashisms", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_one_file_as_nautilus_passes_it(tmp_path, shell):
    """Inside the folder Nautilus passes a name relative to it and runs the script there."""
    h = Home(tmp_path)
    (photo,) = h.files(["Photo.jpg"])
    argv, launcher = h.run(shell, "Photo.jpg", **_nautilus_env([photo]))
    assert argv == [str(photo)]
    assert launcher == str(h.bin / "dragshift")


@pytest.mark.parametrize("shell", SHELLS)
def test_every_name_arrives_exactly_in_one_launch(tmp_path, shell):
    h = Home(tmp_path)
    files = h.files()
    elsewhere = tmp_path / "elsewhere.pdf"      # outside the folder: Nautilus passes it whole
    elsewhere.write_bytes(b"x")
    args = [f.name for f in files] + [str(elsewhere)]
    argv, _ = h.run(shell, *args, **_nautilus_env([*files, elsewhere]))
    assert argv == [str(f) for f in files] + [str(elsewhere)]


@pytest.mark.parametrize("shell", SHELLS)
def test_without_arguments_the_uris_are_decoded(tmp_path, shell):
    """A caller that sets only the variables (Nautilus passes no arguments when a selected
    file has no local path): the URIs are exact, the non-file ones are skipped."""
    h = Home(tmp_path)
    files = h.files()
    env = _nautilus_env(files)
    env["NAUTILUS_SCRIPT_SELECTED_URIS"] += "trash:///gone.jpg\nsftp://host/x.jpg\n"
    argv, _ = h.run(shell, **env)
    assert argv == [str(f) for f in files]


@pytest.mark.parametrize("shell", SHELLS)
def test_only_the_file_paths_variable(tmp_path, shell):
    """Two paths in NAUTILUS_SCRIPT_SELECTED_FILE_PATHS alone. There a name containing a
    newline cannot be told apart, so it is dropped (the header says so)."""
    h = Home(tmp_path)
    a, b, nl = h.files(["Photo.jpg", "two words.jpg", "new\nline.jpg"])
    argv, _ = h.run(shell, NAUTILUS_SCRIPT_SELECTED_FILE_PATHS=f"{a}\n{b}\n{nl}\n")
    assert argv == [str(a), str(b)]


@pytest.mark.parametrize("shell", SHELLS)
def test_folder_background_brings_dragshift_up(tmp_path, shell):
    """No selection: the launcher runs without files, which sends {"show": true}."""
    h = Home(tmp_path)
    h.files()
    argv, _ = h.run(shell, NAUTILUS_SCRIPT_SELECTED_FILE_PATHS="",
                    NAUTILUS_SCRIPT_SELECTED_URIS="")
    assert argv == []
    argv, _ = h.run(shell, NAUTILUS_SCRIPT_SELECTED_URIS="trash:///x.jpg\n")
    assert argv == []


def test_which_dragshift_runs(tmp_path):
    """~/.local/bin/dragshift, else dragshift on PATH, else python -m dragshift from
    DRAGSHIFT_SRC, else a message and exit 1."""
    h = Home(tmp_path, launcher=False)
    (photo,) = h.files(["Photo.jpg"])
    on_path = h.fake(tmp_path / "bin" / "dragshift")
    path = f"{on_path.parent}:/usr/bin:/bin"
    assert h.run("sh", "Photo.jpg", path=path) == ([str(photo)], str(on_path))
    mine = h.fake(h.bin / "dragshift")
    assert h.run("sh", "Photo.jpg", path=path) == ([str(photo)], str(mine))

    mine.unlink()
    src = tmp_path / "src"
    main = src / "dragshift" / "__main__.py"
    main.parent.mkdir(parents=True)
    main.write_text(f"import json, os, sys\njson.dump([sys.argv[1:], os.getcwd(), "
                    f"sys.executable], open({str(h.record)!r}, 'w'))\n")
    venv_python = src / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    argv, python = h.run("sh", "Photo.jpg", DRAGSHIFT_SRC=str(src))
    assert argv == [str(photo)] and python == str(venv_python)

    r = subprocess.run([str(SCRIPT), "Photo.jpg"], cwd=h.folder,
                       env={"HOME": str(h.home), "PATH": "/usr/bin:/bin",
                            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "DragShift is not installed" in r.stderr


def test_through_the_launcher_to_the_running_app(runtime, tmp_path):  # noqa: F811
    """The whole chain without a desktop: the script runs the real launcher, which runs
    `python -m dragshift`, which hands every file to the socket in one message."""
    h = Home(tmp_path, launcher=False)
    shutil.copy(LAUNCHER, h.bin / "dragshift")
    files = h.files()
    from dragshift import handoff
    fake = FakeApp(handoff.socket_path())
    try:
        env = {"HOME": str(h.home), "PATH": os.environ["PATH"],
               "XDG_RUNTIME_DIR": str(runtime), "DRAGSHIFT_VENV": sys.prefix,
               "DRAGSHIFT_SRC": str(ROOT), "QT_QPA_PLATFORM": "no-such-platform",
               **_nautilus_env(files)}
        r = subprocess.run([str(SCRIPT), *(f.name for f in files)], cwd=h.folder,
                           env=env, capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr[-2000:]
        assert fake.wait(1) == [{"files": [str(f) for f in files]}]
    finally:
        fake.close()
