"""install-linux.sh (#66) end to end, in isolated GNOME sessions only. It never runs
against your HOME: every run gets a session's temporary HOME and XDG dirs, its own bus
and runtime dir.

First login (a session on a new root):
- the installer runs from this checkout, as a user would run it (no sudo; a `sudo` on
  PATH records any call and fails). Every file is in place: the launcher (a regular 0755
  file ending in #93's `exec … -m dragshift`), the desktop entry (`desktop-file-validate`
  clean, absolute Exec), the icon, the Files script in both scripts folders, the
  extension (enabled in the settings), the autostart entry. GIO lists DragShift for
  Open With, and the installed app passes `--selftest`;
- it starts DragShift (the Welcome window), and says to log out and in;
- a second run changes no file (checksums), restarts the app, and nothing was written
  outside the session's HOME/XDG dirs; nothing changed in your own HOME.

Second login (a new session on the same root, like logging out and back in):
- the extension is ACTIVE and answers with this version;
- the autostart entry, launched the way gnome-session does it (GDesktopAppInfo), starts
  `<venv>/bin/python -m dragshift --quiet`, the extension lets it in, and a real
  Shift+drag from the desktop opens the menu; the drop on PNG writes Photo.png;
- the app grid (overview search → Return) starts DragShift from the desktop entry, and
  its windows belong to io.github.eteen12.DragShift.desktop;
- Files → Open With… → DragShift and right-click → Scripts → DragShift… open the menu.

Other ways in, each in a fresh session:
- curl mode: `curl -fsSL file://…/install-linux.sh | bash` with DRAGSHIFT_REPO pointing
  at a local git repo of this checkout; then an update from the installed copy (new
  version, venv kept, the autostart choice kept) and a broken venv that gets rebuilt;
- tarball mode without uv (`python3 -m venv` + pip) and with --no-autostart.

Downloads: every run shares one uv/pip cache (DRAGSHIFT_TEST_CACHE, else a temporary
folder per test run), so PySide6 is downloaded once. That cache is the only thing the
installer writes outside the session. With DRAGSHIFT_EVIDENCE_DIR set, screenshots and
the installer's output are written there."""
import hashlib
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dragshift import __version__ as VERSION
from dragshift import handoff
from dragshift.app import LINUX_APP_ID
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session, gi_python, new_root
from tests.linux.shell import ShellHelper
from tests.linux.test_handoff import (close_menu, dragshift_processes, log_since, log_size,
                                      open_with_dragshift, shot, wait_menu)
from tests.linux.test_menu_drop import JPG, Source, _assert_dropped, _expect_one, _shift_drag
from tests.linux.test_menu_drop import img_ok
from tests.linux.test_nautilus_script import check_menu, choose_dragshift

pytestmark = pytest.mark.gnome
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install-linux.sh"
UUID = "dragshift@eteen12.github.io"
DESKTOP_ID = f"{LINUX_APP_ID}.desktop"
SCRIPT_NAME = "DragShift…"
RELOGIN = "Log out and back in once so GNOME loads the DragShift extension."
FOLLOWING = "trigger: following the extension's pointer state"
LAUNCHER_LAST = 'exec "$VENV/bin/python" -m dragshift "$@"'
# What install-linux.sh copies into src (and so what a clone or tarball must hold).
PARTS = ("dragshift", "extension", "packaging", "requirements.txt", "install-linux.sh",
         "LICENSE", "README.md")
# The session root's folders the installer may write to.
ALLOWED = {"home", "data", "config", "state", "cache", "tmp"}
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


# ------------------------------------------------------------------ helpers
def evidence(name: str, data) -> None:
    out = os.environ.get("DRAGSHIFT_EVIDENCE_DIR")
    if not out:
        return
    if isinstance(data, Path):
        shutil.copy(data, Path(out) / name)
    else:
        (Path(out) / name).write_text(data)


def paths(e: dict) -> SimpleNamespace:
    home, data, config = Path(e["HOME"]), Path(e["XDG_DATA_HOME"]), Path(e["XDG_CONFIG_HOME"])
    return SimpleNamespace(
        home=home, data=data, config=config, state=Path(e["XDG_STATE_HOME"]),
        launcher=home / ".local" / "bin" / "dragshift",
        venv=data / "dragshift" / "venv", src=data / "dragshift" / "src",
        desktop=data / "applications" / DESKTOP_ID,
        mimecache=data / "applications" / "mimeinfo.cache",
        icon=data / "icons" / "hicolor" / "256x256" / "apps" / f"{LINUX_APP_ID}.png",
        scripts=[data / "nautilus" / "scripts" / SCRIPT_NAME,
                 home / ".local" / "share" / "nautilus" / "scripts" / SCRIPT_NAME],
        ext=data / "gnome-shell" / "extensions" / UUID,
        autostart=config / "autostart" / "dragshift.desktop",
        log=Path(e["XDG_STATE_HOME"]) / "DragShift" / "log.txt")


def in_session(s, shell) -> SimpleNamespace:
    """What tests/linux/test_handoff.py's helpers take as `env`."""
    e = s.env()
    return SimpleNamespace(session=s, inside=s, shell=shell, home=Path(e["HOME"]),
                           data=Path(e["XDG_DATA_HOME"]), socket=Path(handoff.socket_path(e)),
                           log=Path(e["XDG_STATE_HOME"]) / "DragShift" / "log.txt")


def entry(path: Path) -> dict:
    keys = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        k, _, v = line.partition("=")
        keys[k] = v
    return keys


def digest(p: SimpleNamespace) -> dict[str, str]:
    """Mode and sha256 (or link target) of every file the installer writes. Bytecode the
    app compiles at run time is left out."""
    out = {}

    def add(path: Path) -> None:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            what = "-> " + os.readlink(path)
        else:
            what = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path)] = f"{stat.filemode(st.st_mode)} {what}"

    for f in (p.launcher, p.desktop, p.mimecache, p.icon, *p.scripts, p.autostart):
        if f.exists():
            add(f)
    for tree in (p.src, p.venv, p.ext):
        for f in sorted(tree.rglob("*")):
            if "__pycache__" not in f.parts and (f.is_symlink() or f.is_file()):
                add(f)
    return out


def listing(root: Path) -> set[str]:
    return {str(f.relative_to(root)) for f in root.rglob("*")}


def real_targets() -> dict[str, tuple | None]:
    """inode/mtime/size of what an install would write in your own HOME."""
    share = REAL_HOME / ".local" / "share"
    found = {}
    for f in (REAL_HOME / ".local" / "bin" / "dragshift", share / "dragshift",
              share / "applications" / DESKTOP_ID, share / "applications" / "mimeinfo.cache",
              share / "icons" / "hicolor" / "256x256" / "apps" / f"{LINUX_APP_ID}.png",
              share / "nautilus" / "scripts" / SCRIPT_NAME,
              share / "gnome-shell" / "extensions" / UUID,
              REAL_HOME / ".config" / "autostart" / "dragshift.desktop"):
        try:
            st = f.lstat()
            found[str(f)] = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            found[str(f)] = None
    return found


def wait(fn, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def argv_of(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]


def stop_apps(env) -> None:
    for pid in dragshift_processes(env):
        os.kill(pid, 15)
    wait(lambda: not dragshift_processes(env), 15, "DragShift did not quit")


def snapshot_tree(dest: Path) -> None:
    """Copy what the installer installs from this checkout to dest."""
    for part in PARTS:
        src = ROOT / part
        if src.is_dir():
            shutil.copytree(src, dest / part, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.exists():
            shutil.copy2(src, dest / part)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    """A `sudo` that records calls and fails, and the shared download cache."""
    require_session_tools()
    base = tmp_path_factory.mktemp("installer")
    (base / "bin").mkdir()
    marker = base / "sudo-calls"
    (base / "bin" / "sudo").write_text(f'#!/bin/sh\necho "sudo $*" >> "{marker}"\nexit 1\n')
    (base / "bin" / "sudo").chmod(0o755)
    cache = Path(os.environ.get("DRAGSHIFT_TEST_CACHE") or base / "cache")
    return SimpleNamespace(bin=base / "bin", sudo_calls=marker, cache=cache, base=base)


def run_installer(s, tools, *args, cmd=None, extra=None, path=None, tag=None):
    e = {"UV_CACHE_DIR": str(tools.cache / "uv"), "PIP_CACHE_DIR": str(tools.cache / "pip"),
         "PATH": f"{tools.bin}:{path or s.env()['PATH']}"}
    e.update(extra or {})
    t0 = time.monotonic()
    r = s.run(cmd or ["bash", str(SCRIPT), *args], env=e, cwd=s.env()["HOME"],
              stdin=subprocess.DEVNULL, timeout=1200)
    took = time.monotonic() - t0
    text = f"$ {' '.join(cmd or ['install-linux.sh', *args])}\n{r.stdout}{r.stderr}"
    print(f"\n{text}(exit {r.returncode}, {took:.0f} s)")
    if tag:
        evidence(f"{tag}.txt", text + f"(exit {r.returncode}, {took:.0f} s)\n")
    return r


@pytest.fixture(scope="module")
def root():
    require_session_tools()
    r = new_root()
    yield r
    shutil.rmtree(r, ignore_errors=True)


@pytest.fixture(scope="module")
def first_login(root, tools):
    """Log in, run the installer twice, log out. Returns what was seen."""
    real_before = real_targets()
    res = SimpleNamespace(real_before=real_before)
    with Session(root=root, helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        env = in_session(s, shell)
        p = res.paths = paths(s.env())
        before = listing(root)
        res.run1 = run_installer(s, tools, tag="install-1-first-run")
        assert res.run1.returncode == 0, res.run1.stdout + res.run1.stderr
        res.files1 = digest(p)
        # The installer started DragShift. The extension only loads at the next login.
        res.started = list(wait(lambda: dragshift_processes(env), 30,
                                "the installer did not start DragShift").values())
        res.welcome = wait(lambda: [w for w in shell.list_windows()
                                    if w["title"].startswith("Welcome") and w["frame"][2] > 0],
                           30, "no Welcome window")
        time.sleep(1.5)
        shot(env, "install-1-welcome")
        res.enabled = s.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"]).stdout
        res.info = s.run(["gnome-extensions", "info", UUID])
        res.selftest = s.run([str(p.launcher), "--selftest"],
                             env={"QT_QPA_PLATFORM": "offscreen"}, timeout=180)
        res.validate = {f.name: s.run(["desktop-file-validate", str(f)])
                        for f in (p.desktop, p.autostart)}
        res.gio_mime = s.run(["gio", "mime", "image/png"]).stdout
        first_pids = set(dragshift_processes(env))
        res.run2 = run_installer(s, tools, tag="install-2-second-run")
        assert res.run2.returncode == 0, res.run2.stdout + res.run2.stderr
        res.files2 = digest(p)
        res.restarted = wait(lambda: set(dragshift_processes(env)) - first_pids, 30,
                             "DragShift was not started again")
        res.outside = sorted(f for f in listing(root) - before
                             if f.split("/")[0] not in ALLOWED)
        res.app_log = p.log.read_text(errors="replace")
    res.real_after = real_targets()
    res.sudo_calls = tools.sudo_calls.read_text() if tools.sudo_calls.exists() else ""
    # During that login the user closed the Welcome window ("Got it").
    conf = p.config / "DragShift" / "DragShift.conf"
    text = conf.read_text() if conf.exists() else ""
    if "welcomed=" not in text:
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(text.replace("[General]\n", "[General]\nwelcomed=true\n", 1)
                        if "[General]\n" in text else "[General]\nwelcomed=true\n" + text)
    return res


@pytest.fixture(scope="module")
def second_login(root, first_login):
    """Log in again on the same HOME."""
    with Session(root=root, helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        env = in_session(s, shell)
        env.paths = paths(s.env())
        env.app_pid = None
        yield env
        shell.release_all()


# ------------------------------------------------------------------ first login
def test_installer_runs_without_sudo_and_asks_for_a_new_login(first_login):
    out = first_login.run1.stdout + first_login.run1.stderr
    assert f"→ Installing from {ROOT}" in out
    assert "Creating the Python environment" in out
    assert f"✓ DragShift {VERSION} is installed." in out
    assert RELOGIN in out
    assert first_login.sudo_calls == ""                 # never ran sudo
    assert "sudo" not in out                            # and never asked for it


def test_every_file_is_in_place(first_login):
    p, res = first_login.paths, first_login
    st = p.launcher.lstat()
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o755
    assert st.st_uid == os.getuid()
    lines = p.launcher.read_text().splitlines()
    assert lines[-1] == LAUNCHER_LAST                   # #93: the extension only answers this
    assert f"DATA='{p.data / 'dragshift'}'" in lines
    # desktop entry: validates, absolute launcher (no PATH needed), its icon
    for name, r in res.validate.items():
        assert r.returncode == 0 and r.stdout == r.stderr == "", (name, r.stdout + r.stderr)
    d = entry(p.desktop)
    assert d["Exec"] == f"{p.launcher} %F"
    assert d["Icon"] == LINUX_APP_ID
    with Image.open(p.icon) as im:
        assert (im.format, im.size) == ("PNG", (256, 256))
    packaged = (ROOT / "packaging" / "linux" / "nautilus-scripts" / SCRIPT_NAME).read_bytes()
    for script in p.scripts:            # Files' and the desktop's scripts folders
        assert script.read_bytes() == packaged
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert DESKTOP_ID in res.gio_mime.split("Recommended applications:")[-1]
    # the extension, stamped with the app version (#22), turned on for the next login
    meta = (p.ext / "metadata.json").read_text()
    assert f'"version-name": "{VERSION}"' in meta
    assert {f.name for f in p.ext.iterdir()} >= {"extension.js", "caller.js", "placement.js",
                                                  "dbus.xml", "metadata.json"}
    assert UUID in res.enabled
    # autostart (#47's format, written by the installed app's own code)
    a = entry(p.autostart)
    assert a["Exec"] == f"{p.launcher} --quiet" and a["X-GNOME-Autostart-enabled"] == "true"
    assert "SELFTEST OK" in res.selftest.stdout + res.selftest.stderr
    assert (p.src / "dragshift" / "__init__.py").read_bytes() == \
        (ROOT / "dragshift" / "__init__.py").read_bytes()
    assert not (p.src / "tests").exists() and not (p.src / ".git").exists()


def test_the_installer_starts_dragshift(first_login):
    p, res = first_login.paths, first_login
    assert res.started == [[str(p.venv / "bin" / "python"), "-m", "dragshift", "--quiet"]]
    assert res.welcome                                   # a fresh user: the Welcome window
    # The running shell doesn't know an extension installed after it started (GNOME 46),
    # hence the log-out-and-in note.
    assert res.info.returncode != 0, res.info.stdout
    print(f"\ngnome-extensions info right after the install: {res.info.stdout.strip()}"
          f"{res.info.stderr.strip()}")


def test_running_it_again_changes_nothing(first_login):
    res = first_login
    out = res.run2.stdout + res.run2.stderr
    assert "Creating the Python environment" not in out          # the venv is kept
    assert "Stopping the running DragShift" in out               # and the app restarted
    assert res.restarted
    changed = sorted(k for k in res.files1.keys() | res.files2.keys()
                     if res.files1.get(k) != res.files2.get(k))
    assert changed == [], changed
    print(f"\n{len(res.files1)} installed files, identical after the second run")


def test_nothing_is_written_outside_the_session_home(first_login):
    assert first_login.outside == []
    assert first_login.real_after == first_login.real_before


# ------------------------------------------------------------------ second login
def test_the_extension_is_active_after_logging_in_again(second_login):
    s = second_login.session

    def info():
        r = s.run(["gnome-extensions", "info", UUID])
        return r.stdout if "State: ACTIVE" in r.stdout else None
    print("\n" + wait(info, 20, "the extension is not ACTIVE"))
    assert UUID in s.run(["gnome-extensions", "list", "--enabled"]).stdout
    r = s.run(["gdbus", "call", "--session", "--dest", "io.github.eteen12.DragShift",
               "--object-path", "/io/github/eteen12/DragShift",
               "--method", "io.github.eteen12.DragShift.Version"])
    assert r.stdout.strip() == f"('{VERSION}',)", r.stdout + r.stderr


# gnome-session starts autostart entries through GDesktopAppInfo; the headless session has
# no gnome-session, so this does what it does: honour Hidden, OnlyShowIn/NotShowIn and
# X-GNOME-Autostart-enabled, then launch.
AUTOSTART = r"""
import sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio
info = Gio.DesktopAppInfo.new_from_filename(sys.argv[1])
assert not info.get_is_hidden() and info.get_show_in(None), "gnome-session would skip it"
if info.has_key("X-GNOME-Autostart-enabled"):
    assert info.get_boolean("X-GNOME-Autostart-enabled"), "disabled"
print(info.get_commandline(), flush=True)
info.launch([], None)
"""


def test_autostart_starts_dragshift_and_shift_drag_opens_the_menu(second_login):
    env, s, shell, p = second_login, second_login.session, second_login.shell, second_login.paths
    assert not dragshift_processes(env)
    out = s.root / "tmp" / "autostart.out"
    with open(out, "w") as f:
        r = s.popen([gi_python(), "-I", "-c", AUTOSTART, str(p.autostart)], stdout=f,
                    stderr=subprocess.STDOUT)
        assert r.wait(30) == 0, out.read_text()
    text = lambda: out.read_text(errors="replace")         # noqa: E731
    pid, argv = next(iter(wait(lambda: dragshift_processes(env), 30, text()).items()))
    assert argv == [str(p.venv / "bin" / "python"), "-m", "dragshift", "--quiet"]
    wait(lambda: FOLLOWING in text(), 30, "the extension did not answer the app: " + text())
    files = Files(s, shell)
    app = {"proc": SimpleNamespace(pid=pid), "text": text}
    src = Source(files, "desktop", {"Photo.jpg": JPG})
    outs = []
    try:
        log, _ = _shift_drag(s, shell, app, src, "Photo.jpg", to=("Convert", "PNG"),
                             tag="relogin-autostart-drag")
        assert "menu: 1 file(s)" in log
        _assert_dropped(log, 1, "PNG", "Convert")
        outs = [_expect_one(src, "Photo.png")]
        assert img_ok(outs[0], "PNG") == (64, 48)
        shot(env, "relogin-autostart-result")
        src.check_untouched()
    finally:
        src.cleanup(outs)
        files.close()
        shell.release_all()
        evidence("relogin-autostart-app.txt", text())
        stop_apps(env)


def test_the_app_grid_starts_dragshift(second_login):
    env, shell, p = second_login, second_login.shell, second_login.paths
    assert not dragshift_processes(env)
    inject.glide(shell, 800, 450, 0.2)
    inject.press(shell, "Super_L")                       # the overview
    time.sleep(1.5)
    inject.type_text(shell, "DragShift")                 # search the app grid's apps
    time.sleep(2.0)
    shot(env, "relogin-app-grid-search")
    t0 = time.monotonic()
    inject.press(shell, "Return")
    pid, argv = next(iter(wait(lambda: dragshift_processes(env), 30,
                               "the app grid did not start DragShift").items()))
    print(f"\napp grid: DragShift running {(time.monotonic() - t0) * 1000:.0f} ms after Return")
    assert argv == [str(p.venv / "bin" / "python"), "-m", "dragshift"]   # Exec=… %F, no file
    # Its windows (the "DragShift is running" toast) belong to the desktop entry.
    wins = wait(lambda: [w for w in shell.list_windows() if w["wm_class"] == LINUX_APP_ID
                         and w["frame"][2] > 0], 20, "no DragShift window")
    assert {w["app"] for w in wins} == {DESKTOP_ID}, wins
    time.sleep(1.0)
    shot(env, "relogin-app-grid-started")
    second_login.app_pid = pid


@pytest.fixture
def pics(second_login):
    d = second_login.home / f"pics-{time.monotonic_ns()}"
    d.mkdir()
    for name, color in (("Photo.jpg", (200, 40, 40)), ("Second.jpg", (40, 160, 40))):
        Image.new("RGB", (64, 48), color).save(d / name, "JPEG", quality=95)
    return d


def _running(env) -> int:
    if env.app_pid not in dragshift_processes(env):
        pytest.skip("needs the app the app grid started (test_the_app_grid_starts_dragshift)")
    return env.app_pid


@pytest.mark.xfail(not Path("/usr/share/themes/Yaru/gtk-4.0").is_dir(), strict=False,
                   reason="open_with_dragshift finds the selected row by Yaru's tint (#125)")
def test_files_open_with_dragshift(second_login, pics):
    env = second_login
    pid = _running(env)
    log_from = log_size(env)
    files = Files(env.session, env.shell)
    try:
        t0 = open_with_dragshift(env, files, pics, "installed")
        win = wait_menu(env, timeout=30)
        print(f"\nOpen With: menu {(win['seen'] - t0) * 1000:.0f} ms after Open")
        assert win["pid"] == pid and list(dragshift_processes(env)) == [pid]
        time.sleep(0.6)
        shot(env, "relogin-open-with-menu")
        log = log_since(env, log_from)
        assert log.count("click menu: ") == 1 and "click menu: 2 file(s)" in log, log
        close_menu(env)
    finally:
        files.close()
        env.shell.release_all()


def test_files_scripts_dragshift(second_login, pics):
    env = second_login
    pid = _running(env)
    files = Files(env.session, env.shell)
    try:
        files.open_folder(pics, view="icon")
        photo = files.icon_center("Photo.jpg")
        inject.click(env.shell, *photo)
        time.sleep(0.3)
        log_from = log_size(env)
        t0 = choose_dragshift(env, photo, "installed")
        win = check_menu(env, t0, 1, log_from, "installed")
        assert win["pid"] == pid
        close_menu(env)
    finally:
        files.close()
        env.shell.release_all()


# ------------------------------------------------------------------ other ways in
@pytest.fixture
def fresh_session():
    require_session_tools()
    with Session() as s:
        yield s


def _bump_version(tree: Path, version: str) -> None:
    init = tree / "dragshift" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{VERSION}"', f'"{version}"'))


@pytest.mark.skipif(not shutil.which("git"), reason="needs git for a local clone URL")
def test_curl_one_liner_clones_then_updates(fresh_session, tools, tmp_path):
    s = fresh_session
    p = paths(s.env())
    repo = tmp_path / "DragShift"
    repo.mkdir()
    snapshot_tree(repo)
    git = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "snapshot"]):
        subprocess.run(git + cmd, check=True, capture_output=True)
    extra = {"DRAGSHIFT_REPO": f"file://{repo}"}
    one_liner = ["bash", "-c", 'curl -fsSL "file://$1" | bash -s -- --no-start', "bash",
                 str(repo / "install-linux.sh")]
    r = run_installer(s, tools, cmd=one_liner, extra=extra, tag="install-curl-1")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert f"→ Downloading DragShift (main) from file://{repo}" in out
    assert "Starting DragShift" not in out and not dragshift_processes(SimpleNamespace(data=p.data))
    assert (p.src / "dragshift" / "__init__.py").read_text() == \
        (repo / "dragshift" / "__init__.py").read_text()
    assert entry(p.autostart)["Exec"] == f"{p.launcher} --quiet"    # first install: on

    # An update from the installed copy downloads the new version; the venv stays and so
    # does the user's "don't start at login" from Settings.
    _bump_version(repo, "9.9.9")
    subprocess.run(git + ["commit", "-qam", "9.9.9"], check=True, capture_output=True)
    p.autostart.unlink()
    venv_id = (p.venv / "pyvenv.cfg").stat().st_ino
    r = run_installer(s, tools, cmd=["bash", str(p.src / "install-linux.sh"), "--no-start"],
                      extra=extra, tag="install-curl-2-update")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Downloading DragShift" in out and "✓ DragShift 9.9.9 is installed." in out
    assert "Creating the Python environment" not in out
    assert (p.venv / "pyvenv.cfg").stat().st_ino == venv_id
    assert '"version-name": "9.9.9"' in (p.ext / "metadata.json").read_text()
    assert not p.autostart.exists()

    # A venv whose Python is gone (say, after a system upgrade) is rebuilt.
    (p.venv / "bin" / "python").unlink()
    (p.venv / "bin" / "python").symlink_to("/nonexistent/python3")
    r = run_installer(s, tools, cmd=one_liner, extra=extra, tag="install-curl-3-broken-venv")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Creating the Python environment" in out
    st = s.run([str(p.launcher), "--selftest"], env={"QT_QPA_PLATFORM": "offscreen"}, timeout=180)
    assert "SELFTEST OK" in st.stdout + st.stderr


def test_tarball_without_uv_and_no_autostart(fresh_session, tools, tmp_path):
    s = fresh_session
    p = paths(s.env())
    tree = tmp_path / "tree"
    tree.mkdir()
    snapshot_tree(tree)
    tarball = tmp_path / "main.tar.gz"
    with tarfile.open(tarball, "w:gz") as t:      # GitHub's layout: one top folder
        for part in PARTS:
            if (tree / part).exists():
                t.add(tree / part, arcname=f"DragShift-main/{part}")
    no_uv = ":".join(d for d in s.env()["PATH"].split(":") if d and not (Path(d) / "uv").exists())
    cmd = ["bash", "-c", 'curl -fsSL "file://$1" | bash -s -- --no-start --no-autostart', "bash",
           str(SCRIPT)]
    r = run_installer(s, tools, cmd=cmd, path=no_uv,
                      extra={"DRAGSHIFT_TARBALL": f"file://{tarball}"}, tag="install-tarball-pip")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert f"→ Downloading DragShift from file://{tarball}" in out
    assert "→ Creating the Python environment\n" in out          # python3 -m venv, no uv
    assert (p.venv / "bin" / "pip").exists()
    assert not p.autostart.exists()
    assert "doesn't start when you log in" in out
    assert p.launcher.read_text().splitlines()[-1] == LAUNCHER_LAST
    st = s.run([str(p.launcher), "--selftest"], env={"QT_QPA_PLATFORM": "offscreen"}, timeout=180)
    assert "SELFTEST OK" in st.stdout + st.stderr
    assert tools.sudo_calls.exists() is False or tools.sudo_calls.read_text() == ""
