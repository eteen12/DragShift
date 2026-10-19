"""install-linux.sh --uninstall (#67) after a real install, in isolated GNOME sessions only:
every run uses a session's temporary HOME and XDG dirs, its own bus and dconf. Three logins
on one session root, like a user logging out and back in:

1. The user's own things are there first: `Open in Claude Code` in both scripts folders
   (Files' and the desktop's) and another extension, on in enabled-extensions. Then
   install → the app runs → uninstall: the app quits over its socket ({"quit": true}),
   every installed file is gone (the file tree before the install and after the uninstall
   differ by nothing of DragShift's but the kept settings and log), the user's things are
   byte-identical, only our UUID left enabled-extensions. A second uninstall has nothing
   to remove. A re-install works.
2. Log in again: the re-installed extension is ACTIVE and the app follows it. Uninstall
   --purge with the extension loaded: the shell switches it off at once (its D-Bus name
   goes), the other extensions stay ACTIVE, settings and log go too.
3. Log in again: GNOME no longer knows the extension; nothing of DragShift's is left.

With DRAGSHIFT_EVIDENCE_DIR set, screenshots and the uninstaller's output go there."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.linux.conftest import require_session_tools
from tests.linux.session import Session, new_root
from tests.linux.shell import ShellHelper
from tests.linux.test_handoff import dragshift_processes, shot
from tests.linux.test_install import (UUID, evidence, in_session, paths, real_targets,
                                      run_installer, tools, wait)  # noqa: F401  (tools: a fixture)

pytestmark = pytest.mark.gnome
OTHER_UUID = "other-test@example.com"
USER_SCRIPT = "Open in Claude Code"
FOLLOWING = "trigger: following the extension's pointer state"
# The folders of a session root that make up the user's home.
HOME_DIRS = ("home", "data", "config", "state", "cache")
# What GNOME itself keeps about every app and file, which may name DragShift's files after
# they existed: the file indexer, Files' metadata and tags, the shell's app usage, the
# shader cache. Not DragShift's files. The dconf database is checked through gsettings.
# Also the session's own test-helper extension (Session(helper=True), never installed by
# the installer).
GNOME_OWNED = ("cache/tracker3", "cache/mesa_shader_cache", "data/gvfs-metadata",
               "data/nautilus/tags", "data/gnome-shell/application_state",
               "data/recently-used.xbel", "config/dconf/user",
               "data/gnome-shell/extensions/dragshift-test@eteen12.github.io")
KEPT = ("config/DragShift", "state/DragShift")

OTHER_EXTENSION_JS = """export default class OtherExtension {
    enable() {}
    disable() {}
}
"""


# ------------------------------------------------------------------ helpers
def snapshot(root: Path) -> dict[str, str]:
    """rel path → mode and sha256 (or link target) of every file and folder of the home.
    A file that GNOME deletes while this runs (an index journal) is left out."""
    out = {}
    for top in HOME_DIRS:
        for f in (root / top).rglob("*"):
            try:
                st = f.lstat()
                if stat.S_ISLNK(st.st_mode):
                    what = "-> " + os.readlink(f)
                elif stat.S_ISDIR(st.st_mode):
                    what = "dir"
                elif stat.S_ISREG(st.st_mode):
                    what = hashlib.sha256(f.read_bytes()).hexdigest()
                else:
                    what = stat.filemode(st.st_mode)[0]
            except FileNotFoundError:
                continue
            out[str(f.relative_to(root))] = f"{stat.filemode(st.st_mode)} {what}"
    return out


def diff(a: dict, b: dict) -> list[str]:
    """Paths added, removed or changed from a to b."""
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def mentions_dragshift(root: Path, rel: str) -> bool:
    f = root / rel
    if "dragshift" in rel.lower():
        return True
    try:
        if not f.is_file() or f.stat().st_size > 5_000_000:
            return False
        data = f.read_bytes().lower()
    except FileNotFoundError:
        return False
    # the session root's own name (/tmp/dragshift-session-…) is in many of GNOME's files
    return b"dragshift" in data.replace(str(root).lower().encode(), b"")


def settings(s, key: str) -> list[str]:
    import ast
    out = s.run(["gsettings", "get", "org.gnome.shell", key]).stdout.strip()
    out = out.removeprefix("@as ")
    return list(ast.literal_eval(out)) if out else []


def ext_state(s, uuid: str) -> str:
    r = s.run(["gnome-extensions", "info", uuid])
    for line in r.stdout.splitlines():
        if line.strip().startswith("State:"):
            return line.split(":", 1)[1].strip()
    return (r.stdout + r.stderr).strip()


def dbus_version(s) -> str:
    r = s.run(["gdbus", "call", "--session", "--timeout", "3", "--dest",
               "io.github.eteen12.DragShift", "--object-path", "/io/github/eteen12/DragShift",
               "--method", "io.github.eteen12.DragShift.Version"])
    return r.stdout.strip() if r.returncode == 0 else ""


def uninstall(s, tools, *args, tag):
    return run_installer(s, tools, "--uninstall", *args, tag=tag)


def users_own(p: SimpleNamespace) -> list[Path]:
    ext = p.data / "gnome-shell" / "extensions" / OTHER_UUID
    return [p.data / "nautilus" / "scripts" / USER_SCRIPT,
            p.home / ".local" / "share" / "nautilus" / "scripts" / USER_SCRIPT,
            ext / "metadata.json", ext / "extension.js"]


def seed_users_own(s, p: SimpleNamespace) -> None:
    script, script_ding, meta, js = users_own(p)
    for f in (script, script_ding):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('#!/bin/sh\nexec claude "$@"\n')
        f.chmod(0o755)
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"uuid": OTHER_UUID, "name": "Other", "description": "A test.",
                                "shell-version": ["46"]}))
    js.write_text(OTHER_EXTENSION_JS)
    on = settings(s, "enabled-extensions")
    s.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions",
           repr(on + [OTHER_UUID])], check=True)


def installed(p: SimpleNamespace) -> list[Path]:
    return [p.launcher, p.venv, p.src, p.desktop, p.icon, *p.scripts, p.ext, p.autostart,
            p.data / "dragshift"]


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def root():
    require_session_tools()
    r = new_root()
    yield r
    shutil.rmtree(r, ignore_errors=True)


@pytest.fixture(scope="module")
def login1(root, tools):
    """Install, run, uninstall, uninstall again, re-install."""
    res = SimpleNamespace(real_before=real_targets())
    with Session(root=root, helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        env = in_session(s, shell)
        p = res.paths = paths(s.env())
        seed_users_own(s, p)
        res.users_before = {str(f): f.read_bytes() for f in users_own(p)}
        res.enabled_before = settings(s, "enabled-extensions")
        res.disabled_before = settings(s, "disabled-extensions")
        res.tree_before = snapshot(root)

        r = run_installer(s, tools, tag="uninstall-0-install")
        assert r.returncode == 0, r.stdout + r.stderr
        res.enabled_installed = settings(s, "enabled-extensions")
        res.app = wait(lambda: dragshift_processes(env), 30, "the installer did not start DragShift")
        wait(lambda: p.log.exists() and "ipc: listening" in p.log.read_text(), 30,
             "the app did not listen on its socket")
        # The user changed a setting meanwhile (Settings writes this file).
        conf = p.config / "DragShift" / "DragShift.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("[General]\nwelcomed=true\njpg_quality=80\n")
        res.conf = conf.read_bytes()
        time.sleep(1.5)
        shot(env, "uninstall-1-before")

        res.run1 = uninstall(s, tools, tag="uninstall-1-uninstall")
        res.after_quit = dragshift_processes(env)
        res.app_log = p.log.read_text(errors="replace")
        time.sleep(0.5)
        shot(env, "uninstall-1-after")
        res.socket_left = env.socket.exists()
        res.enabled_after = settings(s, "enabled-extensions")
        res.disabled_after = settings(s, "disabled-extensions")
        res.tree_after = snapshot(root)
        res.mentions = sorted(k for k in diff(res.tree_before, res.tree_after)
                              if mentions_dragshift(root, k))
        res.kept = {f: f.exists() for f in (conf, p.log)}
        res.conf_after = conf.read_bytes() if conf.exists() else None
        res.users_uninstalled = {str(f): f.read_bytes() for f in users_own(p)}
        res.left = [f for f in installed(p) if f.exists() or f.is_symlink()]
        res.mimecache = (p.data / "applications" / "mimeinfo.cache").read_text() \
            if (p.data / "applications" / "mimeinfo.cache").exists() else ""

        res.run2 = uninstall(s, tools, tag="uninstall-2-again")
        res.tree_again = snapshot(root)
        res.mentions_again = sorted(k for k in diff(res.tree_after, res.tree_again)
                                    if mentions_dragshift(root, k))

        res.run3 = run_installer(s, tools, "--no-start", tag="uninstall-3-reinstall")
        res.reinstalled = [f for f in installed(p) if not f.exists()]
        res.selftest = s.run([str(p.launcher), "--selftest"],
                             env={"QT_QPA_PLATFORM": "offscreen"}, timeout=180)
        res.users_after = {str(f): f.read_bytes() for f in users_own(p)}
    res.real_after = real_targets()
    res.sudo_calls = tools.sudo_calls.read_text() if tools.sudo_calls.exists() else ""
    return res


@pytest.fixture(scope="module")
def login2(root, login1, tools):
    """Log in again: the re-installed extension loads. Then uninstall --purge."""
    res = SimpleNamespace()
    with Session(root=root, helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        env = in_session(s, shell)
        p = res.paths = paths(s.env())
        res.state_before = wait(lambda: ext_state(s, UUID) == "ACTIVE" and "ACTIVE", 20,
                                "the extension is not ACTIVE: " + ext_state(s, UUID))
        res.other_before = wait(lambda: ext_state(s, OTHER_UUID) == "ACTIVE" and "ACTIVE", 20,
                                "the other extension is not ACTIVE")
        res.version_before = dbus_version(s)
        out = root / "tmp" / "app-login2.out"
        with open(out, "w") as f:
            s.popen([str(p.launcher), "--quiet"], cwd=str(p.home), stdout=f,
                    stderr=subprocess.STDOUT)
        res.app = wait(lambda: dragshift_processes(env), 30, "the launcher did not start DragShift")
        wait(lambda: FOLLOWING in out.read_text(errors="replace"), 30,
             "the app did not follow the extension: " + out.read_text(errors="replace"))
        time.sleep(1.0)
        shot(env, "uninstall-4-loaded-before")

        res.run = uninstall(s, tools, "--purge", tag="uninstall-4-purge-loaded")
        res.after_quit = dragshift_processes(env)
        res.state_after = ext_state(s, UUID)
        res.version_after = dbus_version(s)
        res.other_after = ext_state(s, OTHER_UUID)
        res.helper_after = shell.list_windows() is not None          # still answers
        res.enabled_after = settings(s, "enabled-extensions")
        res.disabled_after = settings(s, "disabled-extensions")
        res.left = [f for f in installed(p) + [p.config / "DragShift", p.state / "DragShift"]
                    if f.exists() or f.is_symlink()]
        shot(env, "uninstall-4-loaded-after")
    return res


@pytest.fixture(scope="module")
def login3(root, login2):
    with Session(root=root, helper=True) as s, ShellHelper(s) as shell:
        shell.wait_ready()
        time.sleep(2.0)
        r = s.run(["gnome-extensions", "info", UUID])
        tree = snapshot(root)
        return SimpleNamespace(info=r, other=ext_state(s, OTHER_UUID),
                               listed=s.run(["gnome-extensions", "list"]).stdout,
                               enabled=settings(s, "enabled-extensions"),
                               disabled=settings(s, "disabled-extensions"), tree=tree,
                               mentions=sorted(k for k in tree if not k.startswith(GNOME_OWNED)
                                               and mentions_dragshift(root, k)))


# ------------------------------------------------------------------ login 1
def test_uninstall_quits_the_app_over_its_socket(login1):
    out = login1.run1.stdout + login1.run1.stderr
    assert login1.run1.returncode == 0, out
    assert "→ Quitting DragShift" in out                    # not "Stopping" (SIGTERM)
    assert login1.after_quit == {}
    assert "ipc: quit requested" in login1.app_log
    assert not login1.socket_left                            # Qt removed it on the way out


def test_every_installed_file_is_gone_and_settings_and_log_are_kept(login1):
    p, out = login1.paths, login1.run1.stdout
    assert login1.left == []
    for f in (p.launcher, p.venv, p.src, p.desktop, p.icon, *p.scripts, p.ext, p.autostart,
              p.data / "dragshift"):
        assert f"removed {f}" in out
    assert f"turned the extension {UUID} off" in out
    assert "✓ DragShift is uninstalled." in out
    assert all(login1.kept.values()), login1.kept
    assert login1.conf_after == login1.conf
    assert "Kept your settings and log" in out
    assert "io.github.eteen12.DragShift" not in login1.mimecache   # Open With forgot it
    assert login1.sudo_calls == "" and "sudo" not in out


def test_the_file_tree_has_nothing_of_dragshift_left(login1):
    """Before the install vs after the uninstall, no path that differs names or holds
    DragShift, but GNOME's own records and the settings and log the uninstaller keeps.
    The rest of the difference is shared folders the install created (~/.local/bin,
    applications/, icons/hicolor/…, autostart/: other apps use them, so they stay), Open
    With's mimeinfo.cache (without DragShift now), and what the session itself writes."""
    changed = diff(login1.tree_before, login1.tree_after)
    print(f"\n{len(login1.tree_before)} paths before the install, {len(login1.tree_after)} "
          f"after the uninstall; {len(changed)} differ:")
    for k in changed:
        print(f"  {k}: {login1.tree_before.get(k, '-')} → {login1.tree_after.get(k, '-')}")
    evidence("uninstall-1-tree-diff.txt", "\n".join(
        f"{k}: {login1.tree_before.get(k, '-')} -> {login1.tree_after.get(k, '-')}"
        for k in changed) + "\n")
    left = [k for k in login1.mentions if not k.startswith(GNOME_OWNED + KEPT)]
    assert left == [], left
    gone = [k for k in changed if k not in login1.tree_after and not k.startswith(GNOME_OWNED)]
    assert gone == [], gone                                 # nothing that was there went


def test_the_users_own_scripts_and_extensions_are_untouched(login1):
    assert login1.users_uninstalled == login1.users_before
    assert login1.users_after == login1.users_before        # nor did the re-install touch them
    assert UUID in login1.enabled_installed
    assert login1.enabled_after == login1.enabled_before     # only our UUID left, order kept
    assert OTHER_UUID in login1.enabled_after
    assert login1.disabled_after == login1.disabled_before


def test_a_second_uninstall_has_nothing_to_remove(login1):
    r = login1.run2
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ Nothing to remove: DragShift is not installed for this user." in r.stdout
    assert "removed" not in r.stdout
    assert login1.mentions_again == []


def test_installing_again_works(login1):
    r = login1.run3
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓ DragShift" in r.stdout and "is installed." in r.stdout
    assert login1.reinstalled == []
    assert "SELFTEST OK" in login1.selftest.stdout + login1.selftest.stderr


def test_nothing_outside_the_session_changed(login1):
    assert login1.real_after == login1.real_before


# ------------------------------------------------------------------ login 2
def test_uninstall_switches_a_loaded_extension_off(login2):
    out = login2.run.stdout + login2.run.stderr
    assert login2.run.returncode == 0, out
    assert login2.state_before == "ACTIVE" and login2.version_before
    assert "→ Quitting DragShift" in out and login2.after_quit == {}
    # the shell switched it off at once: its D-Bus service is gone
    assert login2.version_after == "", login2.version_after
    assert login2.state_after != "ACTIVE", login2.state_after
    print(f"\nextension state after the uninstall, same login: {login2.state_after}")
    assert "forgets it completely at your next login" in out
    assert login2.other_after == "ACTIVE" and login2.helper_after
    assert UUID not in login2.enabled_after and UUID not in login2.disabled_after
    assert OTHER_UUID in login2.enabled_after


def test_purge_removes_settings_and_log_too(login2):
    p, out = login2.paths, login2.run.stdout
    assert login2.left == []
    assert f"removed {p.config / 'DragShift'}" in out
    assert f"removed {p.state / 'DragShift'}" in out
    assert "Kept your settings" not in out


# ------------------------------------------------------------------ login 3
def test_after_logging_in_again_gnome_has_forgotten_it(login3, login1):
    assert login3.info.returncode != 0, login3.info.stdout
    assert UUID not in login3.listed
    assert login3.other == "ACTIVE"
    assert login3.enabled == login1.enabled_before
    assert login3.disabled == login1.disabled_before
    assert login3.mentions == [], login3.mentions
