"""Tray behaviour that doesn't need a top bar (#46): the tip link, the fallback without a
StatusNotifier host, the Enabled log line and the icon's frames. Offscreen; Linux branches
are forced with app.IS_LINUX, so this runs on every platform.

The real top bar (AppIndicator in the isolated GNOME session) is tests/linux/test_tray.py."""
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QUrl

from dragshift import app


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def opened(monkeypatch):
    """What open_url started: ("xdg-open", url) or ("qt", QUrl)."""
    seen = []

    def popen(args, **kw):
        seen.append(tuple(args))
    monkeypatch.setattr(app.subprocess, "Popen", popen)
    monkeypatch.setattr(app.QDesktopServices, "openUrl", lambda url: seen.append(("qt", url)))
    return seen


def test_open_url_uses_xdg_open_on_linux(monkeypatch, opened):
    # Qt would hand the URL to the desktop portal, which asks "Open with…"
    monkeypatch.setattr(app, "IS_LINUX", True)
    app.open_url(app.TIP_URL)
    assert opened == [("xdg-open", app.TIP_URL)]


def test_open_url_without_xdg_open_falls_back_to_qt(monkeypatch, opened):
    monkeypatch.setattr(app, "IS_LINUX", True)

    def missing(args, **kw):
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(app.subprocess, "Popen", missing)
    app.open_url(app.TIP_URL)
    assert opened == [("qt", QUrl(app.TIP_URL))]


def test_open_url_elsewhere_unchanged(monkeypatch, opened):
    monkeypatch.setattr(app, "IS_LINUX", False)
    app.open_url(app.TIP_URL)
    assert opened == [("qt", QUrl(app.TIP_URL))]


@pytest.mark.parametrize("linux, available, want", [
    (True, False, True), (True, True, False), (False, False, False), (False, True, False)])
def test_no_tray(monkeypatch, linux, available, want):
    monkeypatch.setattr(app, "IS_LINUX", linux)
    monkeypatch.setattr(app.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: available))
    assert app.no_tray() is want


def _fake_app(calls):
    return SimpleNamespace(
        open_settings=lambda: calls.append("settings"),
        toasts=SimpleNamespace(info=lambda title, text: calls.append((title, text))),
        trigger_label=lambda: "Shift")


def test_relaunch_without_tray_opens_settings(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "no_tray", lambda: True)
    app.DragShiftApp.show_requested(_fake_app(calls))
    assert calls == ["settings"]
    assert app.DragShiftApp.running_text(_fake_app(calls)) == (
        "Drag any file, then press Shift. Open DragShift again for Settings.")


def test_relaunch_with_tray_shows_the_toast(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "no_tray", lambda: False)
    app.DragShiftApp.show_requested(_fake_app(calls))
    assert calls == [("DragShift is already running",
                      "Drag any file and press Shift. It lives in the tray.")]
    assert app.DragShiftApp.running_text(_fake_app(calls)) == "Drag any file, then press Shift."


def test_enabled_toggle_sets_the_watcher_and_logs(monkeypatch):
    lines = []
    monkeypatch.setattr(app, "log_line", lines.append)
    fake = SimpleNamespace(watcher=SimpleNamespace(enabled=True))
    app.DragShiftApp._set_enabled(fake, False)
    assert fake.watcher.enabled is False
    app.DragShiftApp._set_enabled(fake, True)
    assert fake.watcher.enabled is True
    assert lines == ["tray: trigger disabled", "tray: trigger enabled"]


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS uses a template image")
def test_tray_icon_has_exact_top_bar_frames(qapp):
    # GNOME's top bar icon is 16 px; AppIndicator draws the smallest frame of at least
    # 16 × scale, so 16 / 32 / 48 px frames are drawn 1:1 at 100 / 200 / 300 %
    sizes = {(s.width(), s.height()) for s in app.make_tray_icon().availableSizes()}
    assert {(16, 16), (32, 32), (48, 48)} <= sizes
