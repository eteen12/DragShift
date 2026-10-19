"""install-linux.sh --uninstall (#67) without a GNOME session, on a fake install.

Each test lays out the files install-linux.sh writes (and some of the user's own next to
them) in a temporary HOME and XDG dirs, then runs the uninstaller there. gsettings and
gnome-extensions are fakes on PATH that keep their state in files; there is no session bus
or display. A fake `<venv>/bin/python -m dragshift` stands in for the running app.

- exactly the installed paths go; the user's other scripts, extensions, desktop entries,
  launchers and enabled-extensions entries stay; settings and log stay unless --purge;
- partial installs and a second run succeed; a running app is asked to quit over the
  socket, else stopped with SIGTERM (SIGKILL as a last resort); another DragShift is left
  alone; a stale socket is removed;
- an empty, relative or /-like HOME or XDG dir is refused before anything is deleted.

The real install → uninstall round trip runs in the isolated session
(tests/linux/test_uninstall.py)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.test_install_script import ROOT, env, run  # noqa: F401  (env: a fixture)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bash"),
                                reason="install-linux.sh is for Linux")

UUID = "dragshift@eteen12.github.io"
APP_ID = "io.github.eteen12.DragShift"
OTHER_UUID = "other@example.com"


def layout(e: dict) -> dict[str, Path]:
    home, data, config = Path(e["HOME"]), Path(e["XDG_DATA_HOME"]), Path(e["XDG_CONFIG_HOME"])
    return {
        "launcher": home / ".local/bin/dragshift",
        "venv": data / "dragshift/venv",
        "src": data / "dragshift/src",
        "desktop": data / f"applications/{APP_ID}.desktop",
        "icon": data / f"icons/hicolor/256x256/apps/{APP_ID}.png",
        "script": data / "nautilus/scripts/DragShift…",
        "script_ding": home / ".local/share/nautilus/scripts/DragShift…",
        "ext": data / f"gnome-shell/extensions/{UUID}",
        "autostart": config / "autostart/dragshift.desktop",
    }


def kept(e: dict) -> dict[str, Path]:
    """What DragShift itself writes: kept, unless --purge."""
    return {"config": Path(e["XDG_CONFIG_HOME"]) / "DragShift/DragShift.conf",
            "log": Path(e["XDG_STATE_HOME"]) / "DragShift/log.txt",
            "cache": Path(e["XDG_CACHE_HOME"]) / "DragShift/soffice"}


def users_own(e: dict) -> dict[str, Path]:
    """The user's things next to DragShift's."""
    home, data, config = Path(e["HOME"]), Path(e["XDG_DATA_HOME"]), Path(e["XDG_CONFIG_HOME"])
    return {
        "script": data / "nautilus/scripts/Open in Claude Code",
        "script_ding": home / ".local/share/nautilus/scripts/Open in Claude Code",
        "ext": data / f"gnome-shell/extensions/{OTHER_UUID}/metadata.json",
        "desktop": data / "applications/other.desktop",
        "icon": data / "icons/hicolor/256x256/apps/other.png",
        "bin": home / ".local/bin/other-tool",
        "autostart": config / "autostart/other.desktop",
        "data": data / "other-app/state.json",
    }


def fake_install(e: dict, which=None) -> None:
    """Write the installed files (all of them, or the names in `which`)."""
    p = layout(e)
    for name, path in p.items():
        if which is not None and name not in which:
            continue
        if name == "launcher":
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "packaging/linux/dragshift", path)
        elif name == "venv":
            (path / "bin").mkdir(parents=True)
            (path / "bin/python").symlink_to(sys.executable)
            (path / "lib/site.py").parent.mkdir(parents=True)
            (path / "lib/site.py").write_text("")
        elif name == "src":
            (path / "dragshift").mkdir(parents=True)
            (path / "dragshift/__init__.py").write_text('__version__ = "1.1.0"\n')
        elif name == "ext":
            path.mkdir(parents=True)
            (path / "metadata.json").write_text(json.dumps({"uuid": UUID}))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n")


def seed(e: dict, files: dict[str, Path]) -> None:
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n")


def tree(*roots: str) -> dict[str, str]:
    out = {}
    for r in roots:
        for f in sorted(Path(r).rglob("*")):
            if f.is_file() or f.is_symlink():
                out[str(f)] = os.readlink(f) if f.is_symlink() else f.read_text(errors="replace")
    return out


@pytest.fixture(autouse=True)
def _no_dconf(env):  # noqa: F811
    """Should a real gsettings run, it must never reach a dconf database."""
    env["GSETTINGS_BACKEND"] = "memory"


@pytest.fixture
def shell(tmp_path, env):  # noqa: F811
    """Fake gsettings / gnome-extensions on PATH. gsettings keeps org.gnome.shell's keys in
    a JSON file; gnome-extensions logs its calls and knows the extension when told to."""
    bindir = tmp_path / "shellbin"
    bindir.mkdir()
    store, calls = tmp_path / "gsettings.json", tmp_path / "gnome-extensions.calls"
    store.write_text(json.dumps({"enabled-extensions": [OTHER_UUID, UUID, "ubuntu-dock@ubuntu.com"],
                                 "disabled-extensions": ["old@example.com"]}))
    (bindir / "gsettings").write_text(f"""#!{sys.executable}
import ast, json, sys
store = {str(store)!r}
_, cmd, schema, key, *value = sys.argv
assert schema == "org.gnome.shell", schema
data = json.load(open(store))
if cmd == "get":
    v = data[key]
    print(repr(v) if v else "@as []")
elif cmd == "set":
    data[key] = ast.literal_eval(value[0])
    json.dump(data, open(store, "w"))
else:
    sys.exit(1)
""")
    (bindir / "gnome-extensions").write_text(f"""#!/bin/sh
echo "$*" >> {calls}
[ "$1" = info ] && [ -n "$FAKE_SHELL_KNOWS" ] && echo "State: ACTIVE" && exit 0
[ "$1" = disable ] && [ -n "$FAKE_SHELL_KNOWS" ] && exit 0
echo "Extension \\"$2\\" doesn't exist" >&2
exit 2
""")
    for f in bindir.iterdir():
        f.chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return type("Shell", (), {"settings": lambda self: json.loads(store.read_text()),
                              "calls": lambda self: calls.read_text() if calls.exists() else "",
                              "bin": bindir})()


@pytest.fixture
def runtime(env):  # noqa: F811
    """A private XDG_RUNTIME_DIR short enough for a unix socket path."""
    d = tempfile.mkdtemp(prefix="dsrt.", dir="/tmp")
    os.chmod(d, 0o700)
    env["XDG_RUNTIME_DIR"] = d
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# A stand-in for the app: python -m dragshift. "ipc" listens on the hand-off socket and quits
# on {"quit": true} like the real app (dragshift/app.py), "plain" just waits, "deaf"
# also ignores SIGTERM.
FAKE_APP = r'''
import json, os, signal, socket, sys, time
mode, path, out = os.environ["FAKE_MODE"], os.environ["FAKE_SOCKET"], os.environ["FAKE_OUT"]
if mode == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "ipc":
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(4)
    open(out, "a").write("listening\n")
    while True:
        c, _ = s.accept()
        data = b""
        while chunk := c.recv(4096):
            data += chunk
        c.close()
        open(out, "a").write(data.decode() + "\n")
        if json.loads(data).get("quit"):
            s.close()
            os.unlink(path)
            sys.exit(0)
open(out, "a").write("running\n")
while True:
    time.sleep(1)
'''


def start_app(tmp_path, e, runtime, mode, python=None):
    pkg = tmp_path / "fakeapp" / "dragshift"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(FAKE_APP)
    out = tmp_path / f"app-{mode}.out"
    sock = runtime / "DragShift"
    python = python or str(layout(e)["venv"] / "bin/python")
    proc = subprocess.Popen([python, "-m", "dragshift", "--quiet"], cwd=tmp_path,
                            env={**e, "PYTHONPATH": str(tmp_path / "fakeapp"), "FAKE_MODE": mode,
                                 "FAKE_SOCKET": str(sock), "FAKE_OUT": str(out)})
    deadline = time.monotonic() + 10
    while not (out.exists() and out.read_text()):
        assert time.monotonic() < deadline and proc.poll() is None, "the fake app did not start"
        time.sleep(0.05)
    return proc, out, sock


# ------------------------------------------------------------------ what goes, what stays
def test_uninstall_removes_exactly_what_the_installer_wrote(env, shell, tmp_path):  # noqa: F811
    fake_install(env)
    seed(env, kept(env))
    seed(env, users_own(env))
    others_before = tree(*(str(p) for p in users_own(env).values()))
    r = run(env, "--uninstall")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    for name, path in layout(env).items():
        assert not path.exists() and not path.is_symlink(), name
        assert f"removed {path}" in out
    assert not (Path(env["XDG_DATA_HOME"]) / "dragshift").exists()
    assert "✓ DragShift is uninstalled." in out
    # the user's own things, byte for byte
    assert tree(*(str(p) for p in users_own(env).values())) == others_before
    # only our UUID left the settings; the order of the others is kept
    assert shell.settings() == {"enabled-extensions": [OTHER_UUID, "ubuntu-dock@ubuntu.com"],
                                "disabled-extensions": ["old@example.com"]}
    assert f"turned the extension {UUID} off" in out
    # settings and log are kept, and it says how to remove them
    for path in kept(env).values():
        assert path.exists()
    assert "Kept your settings and log" in out and "--uninstall --purge" in out
    assert "sudo" not in out


def test_purge_also_removes_settings_log_and_cache(env, shell):  # noqa: F811
    fake_install(env)
    seed(env, kept(env))
    seed(env, users_own(env))
    snap = Path(env["HOME"]) / "snap/libreoffice/common/DragShift"
    snap.mkdir(parents=True)
    r = run(env, "--uninstall", "--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    for d in (Path(env["XDG_CONFIG_HOME"]) / "DragShift", Path(env["XDG_STATE_HOME"]) / "DragShift",
              Path(env["XDG_CACHE_HOME"]) / "DragShift", snap):
        assert not d.exists(), d
        assert f"removed {d}" in r.stdout
    assert snap.parent.is_dir()                       # the snap's own folder stays
    assert all(p.exists() for p in users_own(env).values())
    assert "Kept your settings" not in r.stdout


def test_a_second_run_and_a_clean_home_have_nothing_to_remove(env, shell):  # noqa: F811
    fake_install(env)
    assert run(env, "--uninstall").returncode == 0
    before = tree(env["HOME"], env["XDG_DATA_HOME"], env["XDG_CONFIG_HOME"])
    settings = shell.settings()
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ Nothing to remove: DragShift is not installed for this user." in r.stdout
    assert "removed" not in r.stdout
    assert tree(env["HOME"], env["XDG_DATA_HOME"], env["XDG_CONFIG_HOME"]) == before
    assert shell.settings() == settings


def test_nothing_installed_creates_nothing(env):  # noqa: F811
    r = run(env, "--uninstall", "--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Nothing to remove" in r.stdout
    assert [p for p in Path(env["HOME"]).rglob("*")] == []
    for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert not Path(env[k]).exists()


@pytest.mark.parametrize("which", [{"launcher"}, {"venv", "desktop"}, {"src", "ext", "autostart"},
                                   {"script_ding", "icon"}])
def test_a_partial_install(env, shell, which):  # noqa: F811
    fake_install(env, which)
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    for name in which:
        assert f"removed {layout(env)[name]}" in r.stdout
    # each one, plus the emptied $XDG_DATA_HOME/dragshift
    assert r.stdout.count("removed ") == len(which) + ("venv" in which or "src" in which)
    assert not any(p.exists() for p in layout(env).values())


def test_only_the_settings_entry_is_left(env, shell):  # noqa: F811
    r = run(env, "--uninstall")
    assert r.returncode == 0
    assert UUID not in shell.settings()["enabled-extensions"]
    assert f"turned the extension {UUID} off" in r.stdout and "✓ DragShift is uninstalled." in r.stdout


def test_data_folder_with_other_files_is_kept(env, shell):  # noqa: F811
    fake_install(env)
    mine = Path(env["XDG_DATA_HOME"]) / "dragshift/notes.txt"
    mine.write_text("mine")
    r = run(env, "--uninstall")
    assert r.returncode == 0
    assert mine.read_text() == "mine"
    assert not layout(env)["venv"].exists() and not layout(env)["src"].exists()
    assert "Kept " + str(mine.parent) in r.stderr


def test_a_launcher_that_is_not_dragshifts_is_kept(env, shell):  # noqa: F811
    p = layout(env)["launcher"]
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\necho someone else's dragshift\n")
    r = run(env, "--uninstall")
    assert r.returncode == 0 and p.exists()
    assert f"Kept {p}: it isn't DragShift's launcher." in r.stderr


def test_symlinks_are_removed_not_followed(env, shell, tmp_path):  # noqa: F811
    """An extension folder linked to a checkout (a developer's setup) loses the link only."""
    checkout = tmp_path / "checkout-ext"
    checkout.mkdir()
    (checkout / "extension.js").write_text("// mine")
    ext = layout(env)["ext"]
    ext.parent.mkdir(parents=True)
    ext.symlink_to(checkout)
    r = run(env, "--uninstall")
    assert r.returncode == 0
    assert not ext.is_symlink() and (checkout / "extension.js").read_text() == "// mine"


def test_the_extension_the_shell_has_loaded(env, shell):  # noqa: F811
    """Turned off through the settings the shell watches; a note says GNOME forgets it at the
    next login."""
    env["FAKE_SHELL_KNOWS"] = "1"
    fake_install(env, {"ext"})
    r = run(env, "--uninstall")
    assert r.returncode == 0
    assert "forgets it completely at your next login" in r.stdout
    assert "disable" not in shell.calls()                  # the settings did it


def test_without_gnome_shell_settings_it_asks_the_shell(env, shell):  # noqa: F811
    """No org.gnome.shell schema to edit: `gnome-extensions disable` instead."""
    env["FAKE_SHELL_KNOWS"] = "1"
    (shell.bin / "gsettings").write_text("#!/bin/sh\necho 'No such schema' >&2\nexit 1\n")
    fake_install(env, {"ext"})
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert f"disable {UUID}" in shell.calls()


# ------------------------------------------------------------------ the running app
def test_the_running_app_is_asked_to_quit(env, shell, runtime, tmp_path):  # noqa: F811
    fake_install(env)
    proc, out, sock = start_app(tmp_path, env, runtime, "ipc")
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "→ Quitting DragShift" in r.stdout
    assert proc.wait(5) == 0                              # it quit by itself, no signal
    assert '{"quit": true}' in out.read_text()
    assert not sock.exists()


def test_an_app_that_does_not_answer_is_stopped(env, shell, runtime, tmp_path):  # noqa: F811
    fake_install(env)
    proc, out, sock = start_app(tmp_path, env, runtime, "plain")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock))                                  # a socket left by a crash
    stale.close()
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "→ Stopping DragShift" in r.stdout
    assert proc.wait(5) == -15
    assert f"removed {sock}" in r.stdout and not sock.exists()


def test_an_app_that_ignores_sigterm_is_killed(env, shell, runtime, tmp_path):  # noqa: F811
    fake_install(env)
    proc, _, _ = start_app(tmp_path, env, runtime, "deaf")
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert proc.wait(5) == -9
    assert "still running" not in r.stderr


def test_another_dragshift_is_left_alone(env, shell, runtime, tmp_path):  # noqa: F811
    """A DragShift that isn't this install's (a checkout's: python -m dragshift with another
    Python) keeps running and keeps its socket."""
    fake_install(env)
    proc, out, sock = start_app(tmp_path, env, runtime, "ipc", python=sys.executable)
    try:
        r = run(env, "--uninstall")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Quitting" not in r.stdout and "Stopping" not in r.stdout
        assert proc.poll() is None and sock.exists()
        assert "quit" not in out.read_text()
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------------------------------ guards
@pytest.mark.parametrize("var,value", [
    ("HOME", ""), ("HOME", "/"), ("HOME", "//"), ("HOME", "relative/home"),
    ("XDG_DATA_HOME", "/"), ("XDG_DATA_HOME", "/tmp/.."), ("XDG_CONFIG_HOME", "/./"),
    ("XDG_STATE_HOME", "///"), ("XDG_CACHE_HOME", "/usr/.."),
])
def test_a_home_or_xdg_dir_that_is_root_is_refused(env, shell, var, value):  # noqa: F811
    fake_install(env)
    seed(env, kept(env))
    roots = [env[k] for k in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                              "XDG_CACHE_HOME")]
    before = tree(*roots)
    env[var] = value
    r = run(env, "--uninstall", "--purge", cwd=roots[0])
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout == ""                                  # stopped before doing anything
    assert "refusing" in r.stderr or "absolute" in r.stderr or "not set" in r.stderr, r.stderr
    assert tree(*roots) == before


def test_relative_xdg_values_mean_the_default(env, shell):  # noqa: F811
    """XDG spec: a relative value is ignored, so the uninstaller looks in ~/.local/share."""
    home = Path(env["HOME"])
    env["XDG_DATA_HOME"] = "relative"
    fake_install({**env, "XDG_DATA_HOME": str(home / ".local/share")}, {"src", "ext"})
    r = run(env, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert not (home / ".local/share/dragshift").exists()
    assert not (home / "relative").exists()


@pytest.mark.parametrize("args,msg", [
    (["--purge"], "--purge goes with --uninstall"),
    (["--uninstall", "--no-start"], "--uninstall takes no install options"),
    (["--uninstall", "--from-dir", "/tmp"], "--uninstall takes no install options"),
])
def test_option_mistakes(env, args, msg):  # noqa: F811
    fake_install(env)
    r = run(env, *args)
    assert r.returncode == 1 and msg in r.stderr, r.stderr
    assert all(p.exists() for p in layout(env).values())
