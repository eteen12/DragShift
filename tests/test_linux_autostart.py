"""Start at login on Linux: $XDG_CONFIG_HOME/autostart/dragshift.desktop (#47).

Every test points HOME and XDG_CONFIG_HOME at its own tmp_path, so nothing here can reach
the real ~/.config/autostart."""
import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux autostart")

SYSTEM_PY = "/usr/bin/python3"     # has GLib's introspection bindings on Ubuntu; the venv does not


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))     # does not exist yet
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture
def lx():
    from dragshift import platform_linux
    return platform_linux


def entry(path: Path) -> dict:
    cp = configparser.RawConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    return dict(cp["Desktop Entry"])


def install_launcher(home: Path, where: str = "home") -> Path:
    launcher = home / where / ".local" / "bin" / "dragshift"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return launcher


def glib_argv(path: Path) -> list[str]:
    """Exec as GLib (and so GNOME's autostart) parses it."""
    if not os.path.exists(SYSTEM_PY):
        pytest.skip(f"no {SYSTEM_PY}")
    code = ("import gi, json, sys; gi.require_version('Gio', '2.0'); "
            "from gi.repository import Gio, GLib; "
            "cmd = Gio.DesktopAppInfo.new_from_filename(sys.argv[1]).get_commandline(); "
            "print(json.dumps([a.replace('%%', '%') for a in GLib.shell_parse_argv(cmd)[1]]))")
    r = subprocess.run([SYSTEM_PY, "-c", code, str(path)], capture_output=True, text=True)
    if r.returncode:
        pytest.skip(f"no GLib bindings for {SYSTEM_PY}: {r.stderr.strip()[-200:]}")
    return json.loads(r.stdout)


def test_on_off_round_trip_through_settings(home, lx):
    from dragshift import settings
    path = home / "config" / "autostart" / "dragshift.desktop"
    assert settings.autostart_enabled() is False
    settings.set_autostart(True)                         # config dir missing: created
    assert path.is_file() and lx.autostart_path() == path
    assert settings.autostart_enabled() is True
    settings.set_autostart(True)                         # idempotent
    assert sorted(p.name for p in path.parent.iterdir()) == ["dragshift.desktop"]
    settings.set_autostart(False)
    assert not path.exists() and settings.autostart_enabled() is False
    settings.set_autostart(False)                        # already off: no error


def test_entry_keys_with_launcher(home, lx):
    launcher = install_launcher(home)
    lx.set_autostart(True)
    assert entry(lx.autostart_path()) == {
        "Type": "Application",
        "Name": "DragShift",
        "Comment": "Shift+drag any file to convert or edit it",
        "Exec": f"{launcher} --quiet",
        "Icon": "dragshift",
        "Terminal": "false",
        "X-GNOME-Autostart-enabled": "true",
        "NoDisplay": "false",
    }


def test_entry_without_launcher_runs_this_python(home, lx):
    lx.set_autostart(True)
    e = entry(lx.autostart_path())
    assert lx.launch_args() == [sys.executable, "-m", "dragshift", "--quiet"]
    assert Path(e["Path"]) / "dragshift" / "platform_linux.py" == Path(lx.__file__).resolve()
    assert glib_argv(lx.autostart_path()) == lx.launch_args()


def test_launcher_path_with_spaces_is_quoted(tmp_path, monkeypatch, lx):
    home = tmp_path / "my home $dir"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    launcher = install_launcher(tmp_path, "my home $dir")
    lx.set_autostart(True)
    exec_ = entry(lx.autostart_path())["Exec"]
    escaped = str(launcher).replace("$", "\\\\$")      # quoting gives \$, the string escape \\$
    assert exec_ == '"' + escaped + '" --quiet'
    assert glib_argv(lx.autostart_path()) == [str(launcher), "--quiet"]


def test_exec_quoting(lx):
    q = lx.launch_command
    assert q(["/usr/bin/python3", "-m", "dragshift"]) == "/usr/bin/python3 -m dragshift"
    assert q(["/a b/py"]) == '"/a b/py"'
    assert q(["/100%/py"]) == "/100%%/py"
    assert q(['/x"y/p']) == r'"/x\\"y/p"'               # \" inside quotes, then \ → \\


@pytest.mark.skipif(not shutil.which("desktop-file-validate"), reason="desktop-file-utils not installed")
@pytest.mark.parametrize("launcher", [True, False])
def test_desktop_file_validate_is_clean(home, lx, launcher):
    if launcher:
        install_launcher(home)
    lx.set_autostart(True)
    r = subprocess.run(["desktop-file-validate", str(lx.autostart_path())], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == "", r.stdout + r.stderr


def test_relative_xdg_config_home_means_dot_config(home, lx, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert lx.autostart_path() == home / "home" / ".config" / "autostart" / "dragshift.desktop"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert lx.autostart_path() == home / "home" / ".config" / "autostart" / "dragshift.desktop"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_failures_never_raise(home, lx, capsys):
    (home / "config").mkdir()
    (home / "config" / "autostart").write_text("a file where the folder should be")
    lx.set_autostart(True)
    assert lx.autostart_enabled() is False
    assert "could not enable autostart" in capsys.readouterr().err
    (home / "config" / "autostart").unlink()
    lx.set_autostart(True)
    (home / "config" / "autostart").chmod(0o500)        # read-only folder: delete fails
    try:
        lx.set_autostart(False)
        assert lx.autostart_enabled() is True
        assert "could not disable autostart" in capsys.readouterr().err
    finally:
        (home / "config" / "autostart").chmod(0o700)


def test_launch_command_linux_variant(home, lx):
    from dragshift import settings
    assert settings.launch_command() == f"{sys.executable} -m dragshift --quiet"
    launcher = install_launcher(home)
    assert settings.launch_command() == f"{launcher} --quiet"


# ---------------------------------------------------------------- Settings / Welcome windows
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_settings_checkbox_drives_the_file(home, qapp, lx):
    from dragshift import settings
    w = settings.SettingsWindow(settings.Settings())
    assert w.auto.isChecked() is False
    w.auto.setChecked(True)
    assert lx.autostart_path().is_file()
    w.auto.setChecked(False)
    assert not lx.autostart_path().exists()
    lx.set_autostart(True)
    w2 = settings.SettingsWindow(settings.Settings())
    assert w2.auto.isChecked() is True                 # reflects the file
    w.close(), w2.close()


@pytest.mark.parametrize("checked", [True, False])
def test_welcome_done_applies_checkbox(home, qapp, lx, checked):
    from dragshift import settings
    lx.set_autostart(not checked)
    w = settings.WelcomeWindow(settings.Settings())
    assert w.auto.isChecked() is True                  # default on, as on Windows/macOS
    w.auto.setChecked(checked)
    w._done()
    assert lx.autostart_enabled() is checked
