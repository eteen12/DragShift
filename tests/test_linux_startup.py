"""Linux start-up (#11): Qt platform choice, fonts, app id and the platform log line.

The isolated-session half (real app under GNOME Wayland, scale 1 and 2) is
tests/linux/test_startup.py."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dragshift import app

ROOT = Path(__file__).resolve().parent.parent
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux start-up")


@pytest.mark.parametrize("env, argv, want", [
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}, [], "wayland;xcb"),
    ({"XDG_SESSION_TYPE": "wayland"}, [], "wayland;xcb"),            # default socket
    ({"WAYLAND_DISPLAY": "wayland-0"}, [], "wayland;xcb"),           # no session type
    ({"XDG_SESSION_TYPE": "tty", "WAYLAND_DISPLAY": "wayland-0"}, [], "wayland;xcb"),
    ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, [], None),        # X11: Qt's default
    ({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, [], None),
    ({"DISPLAY": ":0"}, [], None),
    ({}, [], None),
    # the user's own choice always wins
    ({"QT_QPA_PLATFORM": "xcb", "XDG_SESSION_TYPE": "wayland"}, [], None),
    ({"QT_QPA_PLATFORM": "offscreen", "DRAGSHIFT_QPA": "minimal"}, [], None),
    ({"XDG_SESSION_TYPE": "wayland"}, ["dragshift", "-platform", "xcb"], None),
    # DRAGSHIFT_QPA overrides the automatic choice
    ({"DRAGSHIFT_QPA": "minimal", "XDG_SESSION_TYPE": "wayland"}, [], "minimal"),
    ({"DRAGSHIFT_QPA": "xcb", "XDG_SESSION_TYPE": "x11"}, [], "xcb"),
    ({"QT_QPA_PLATFORM": "", "XDG_SESSION_TYPE": "wayland"}, [], "wayland;xcb"),  # empty = unset
])
def test_linux_qpa(env, argv, want):
    assert app.linux_qpa(env, argv) == want


class _Reached(Exception):
    pass


def _qapp_argv(monkeypatch, argv, env, linux=True):
    """The argv main() hands to QApplication (stops right there)."""
    seen = []

    def fake(a):
        seen.append(a)
        raise _Reached
    monkeypatch.setattr(app, "setup_logging", lambda: None)
    monkeypatch.setattr(app, "DragShiftQApplication", fake)
    monkeypatch.setattr(app, "IS_LINUX", linux)
    for k in ("QT_QPA_PLATFORM", "DRAGSHIFT_QPA", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(_Reached):
        app.main(argv)
    return seen[0]


def test_main_asks_for_wayland_by_argument(monkeypatch):
    argv = ["dragshift", "--quiet"]
    got = _qapp_argv(monkeypatch, argv, {"XDG_SESSION_TYPE": "wayland"})
    assert got == ["dragshift", "--quiet", "-platform", "wayland;xcb"]
    assert argv == ["dragshift", "--quiet"]              # the caller's list is untouched
    assert "QT_QPA_PLATFORM" not in os.environ           # children don't inherit it


def test_main_leaves_a_user_platform_alone(monkeypatch):
    argv = ["dragshift"]
    assert _qapp_argv(monkeypatch, argv, {"QT_QPA_PLATFORM": "offscreen",
                                          "XDG_SESSION_TYPE": "wayland"}) is argv


def test_main_unchanged_off_linux(monkeypatch):
    argv = ["dragshift"]
    assert _qapp_argv(monkeypatch, argv, {"XDG_SESSION_TYPE": "wayland",
                                          "DRAGSHIFT_QPA": "minimal"}, linux=False) is argv


@linux_only
def test_linux_setup_fonts_app_id_and_log(monkeypatch, capsys):
    from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
    from PySide6.QtWidgets import QApplication
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app, "_log_file", None)          # log_line → stderr
    app.linux_setup(qapp)
    assert qapp.desktopFileName() == "io.github.eteen12.DragShift"
    err = capsys.readouterr().err
    assert f"platform: {qapp.platformName()}\n" in err
    assert "screen " in err and "output scale" in err
    for fam in ("Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI"):
        assert QFont.substitutes(fam) == [f.lower() for f in app.LINUX_FONTS]  # Qt lowercases
    installed = [f for f in app.LINUX_FONTS if f in QFontDatabase.families()]
    if not installed:
        pytest.skip("none of the Linux UI fonts is installed")
    family = QFontInfo(QFont("Segoe UI Variable Display")).family()
    assert family == installed[0]
    assert f"font: Segoe UI → {family}" in err


def _selftest(tmp_path, **env_over):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM", "DRAGSHIFT_QPA")}
    env.update({k: str(tmp_path / k.lower()) for k in ("HOME", "XDG_STATE_HOME")})
    env.update(env_over)
    r = subprocess.run([sys.executable, "-m", "dragshift", "--selftest"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    log = (tmp_path / "xdg_state_home" / "DragShift" / "log.txt").read_text(encoding="utf-8")
    return r, log


@linux_only
def test_selftest_offscreen_unchanged(tmp_path):
    r, log = _selftest(tmp_path, QT_QPA_PLATFORM="offscreen", XDG_SESSION_TYPE="wayland",
                       DRAGSHIFT_QPA="minimal")
    assert r.returncode == 0 and "SELFTEST OK" in r.stdout, r.stdout + r.stderr
    assert "\nplatform: offscreen\n" in log
    assert "Segoe" not in r.stderr                     # no font warnings


@linux_only
def test_dragshift_qpa_picks_the_platform(tmp_path):
    r, log = _selftest(tmp_path, DRAGSHIFT_QPA="minimal", XDG_SESSION_TYPE="wayland")
    assert r.returncode == 0 and "SELFTEST OK" in r.stdout, r.stdout + r.stderr
    assert "\nplatform: minimal\n" in log
