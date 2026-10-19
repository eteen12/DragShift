"""install-linux.sh (#66) without a GNOME session: options, the system package check and
the quoting helpers. Each run gets a temporary HOME and XDG dirs and no session bus or
display, and every case here stops before anything is installed. The installs
themselves run in the isolated session (tests/linux/test_install.py)."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bash"),
                                reason="install-linux.sh is for Linux")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install-linux.sh"
ONE_LINER = ("curl -fsSL https://raw.githubusercontent.com/eteen12/DragShift/main/"
             "install-linux.sh | bash")


@pytest.fixture
def env(tmp_path):
    """A temporary HOME and XDG dirs, no session bus, no display, no uv."""
    home = tmp_path / "home"
    home.mkdir()
    path = [d for d in os.environ.get("PATH", "/usr/bin:/bin").split(":")
            if d and not (Path(d) / "uv").exists()]
    e = {"HOME": str(home), "PATH": ":".join(path), "LANG": "C.UTF-8",
         "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
         "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
         "XDG_RUNTIME_DIR": str(tmp_path / "run"), "TMPDIR": str(tmp_path),
         "DBUS_SESSION_BUS_ADDRESS": "disabled:"}
    return e


def run(env, *args, cwd=None):
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, cwd=cwd or env["HOME"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


def created(env) -> list[str]:
    """Everything below the temporary HOME and XDG dirs."""
    out = []
    for k in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        if os.path.isdir(env[k]):
            out += [str(p) for p in Path(env[k]).rglob("*")]
    return out


def fake_dpkg(tmp_path, env, missing: set[str]) -> None:
    """A dpkg-query that knows every package but `missing`."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "dpkg-query").write_text(
        "#!/bin/sh\nfor p in \"$@\"; do last=$p; done\n"
        f"case \" {' '.join(sorted(missing))} \" in *\" $last \"*) "
        "echo \"dpkg-query: no packages found matching $last\" >&2; exit 1 ;; esac\n"
        "echo installed\n")
    (bindir / "dpkg-query").chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"


def test_help(env):
    r = run(env, "--help")
    assert r.returncode == 0, r.stderr
    for opt in ("--no-autostart", "--autostart", "--no-start", "--from-dir", "--uninstall"):
        assert opt in r.stdout
    assert ONE_LINER in r.stdout
    assert created(env) == []


def test_unknown_option(env):
    r = run(env, "--frobnicate")
    assert r.returncode == 1 and "Unknown option: --frobnicate" in r.stderr
    assert created(env) == []


def test_uninstall_is_a_stub_that_says_what_to_remove(env):
    r = run(env, "--uninstall")
    assert r.returncode == 2
    assert "issues/67" in r.stderr
    data = env["XDG_DATA_HOME"]
    for path in (f"{env['HOME']}/.local/bin/dragshift", f"{data}/dragshift",
                 f"{data}/applications/io.github.eteen12.DragShift.desktop",
                 f"{data}/nautilus/scripts/DragShift…",
                 f"{env['HOME']}/.local/share/nautilus/scripts/DragShift…",
                 f"{data}/gnome-shell/extensions/dragshift@eteen12.github.io",
                 f"{env['XDG_CONFIG_HOME']}/autostart/dragshift.desktop"):
        assert path in r.stderr
    assert created(env) == []


def test_not_a_checkout(env, tmp_path):
    (tmp_path / "empty").mkdir()
    r = run(env, "--from-dir", str(tmp_path / "empty"))
    assert r.returncode == 1 and "is not a DragShift checkout" in r.stderr
    r = run(env, "--from-dir", str(tmp_path / "missing"))
    assert r.returncode == 1 and "no such folder" in r.stderr
    assert created(env) == []


def test_missing_packages_print_the_apt_line(env, tmp_path):
    """No uv: python3-venv is needed. The Qt libraries and desktop-file-utils are checked;
    an old name (libglib2.0-0) counts as installed. Nothing is installed, no sudo runs."""
    fake_dpkg(tmp_path, env, {"python3-venv", "python3.12-venv", "python3.13-venv",
                              "python3.14-venv", "libegl1", "libwayland-egl1",
                              "libglib2.0-0t64", "desktop-file-utils"})
    r = run(env, "--from-dir", str(ROOT))
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("\n    sudo apt install python3-venv libegl1 libwayland-egl1 desktop-file-utils\n"
            in r.stderr), r.stderr
    assert "run this installer again" in r.stderr
    assert created(env) == []


def test_libxcb_cursor_is_not_required(env, tmp_path):
    """DragShift runs as a native Wayland client: libxcb-cursor0 (Qt's xcb plugin) is not
    on the list, even when it is missing."""
    fake_dpkg(tmp_path, env, {"libxcb-cursor0", "libegl1"})
    r = run(env, "--from-dir", str(ROOT))
    assert r.returncode == 1 and "sudo apt install libegl1\n" in r.stderr, r.stderr


def helpers(code: str, *args: str) -> str:
    """Run bash code with the script's functions sourced; args are $2, $3, …"""
    r = subprocess.run(["bash", "-c", f'source "$1"; {code}', "bash", str(SCRIPT), *args],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_sourcing_runs_nothing(tmp_path):
    assert helpers("echo sourced") == "sourced\n"


@pytest.mark.parametrize("value", ["/home/a b", "/x/it's", "/x/a&b|c\\d", "/x/$HOME`id`",
                                   "/x/é 写真"])
def test_launcher_data_line_keeps_any_path(tmp_path, value):
    """The installer writes the absolute data folder into the launcher's DATA= line; the
    launcher must then see exactly that folder."""
    launcher = helpers('sed "s|^DATA=.*|DATA=$(sed_repl "$(shquote "$2")")|" "$3"', value,
                       str(ROOT / "packaging" / "linux" / "dragshift"))
    lines = launcher.splitlines()
    assert lines[-1] == 'exec "$VENV/bin/python" -m dragshift "$@"'     # #93's form, unchanged
    probe = tmp_path / "probe"
    probe.write_text("\n".join(lines[:-1]) + '\nprintf %s "$VENV"\n')
    r = subprocess.run(["sh", str(probe)], capture_output=True, text=True,
                       env={"HOME": "/nohome", "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{value}/venv"


def test_xdg_dir_ignores_relative_values():
    assert helpers('printf "%s %s %s" "$(xdg_dir relative /d)" "$(xdg_dir "" /d)" '
                   '"$(xdg_dir /abs /d)"') == "/d /d /abs"


def test_readme_documents_the_one_liner():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert ONE_LINER in readme
    assert ONE_LINER in SCRIPT.read_text(encoding="utf-8")


def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# The shellcheck releases install-linux.sh was checked with: Ubuntu 24.04's (the CI runner's)
# 0.9.0, 0.10.0 and 0.11.0. Each release adds checks, so another one is skipped rather than
# failed; run it by hand (`uvx --from shellcheck-py shellcheck -x install-linux.sh`).
SHELLCHECK_TESTED = ((0, 9), (0, 10), (0, 11))


def shellcheck_version() -> tuple[int, ...] | None:
    exe = shutil.which("shellcheck")
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"^version: (\d+)\.(\d+)", out, re.M)
    return (int(m[1]), int(m[2])) if m else (0, 0)


def test_shellcheck_is_clean():
    version = shellcheck_version()
    if version is None:
        pytest.skip("shellcheck not installed")
    if version not in SHELLCHECK_TESTED:
        pytest.skip(f"shellcheck {version[0]}.{version[1]} untested (clean on 0.9-0.11)")
    r = subprocess.run(["shellcheck", "-x", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
