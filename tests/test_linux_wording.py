"""Settings and Welcome speak Linux on Linux; Windows and macOS keep their exact wording (#48).

Offscreen only. Autostart and the settings store are faked, so nothing here writes an autostart
entry, the registry, a LaunchAgent or QSettings."""
import sys

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton

from dragshift import platform_mac, settings

# (Settings checkbox, Welcome checkbox, Welcome step 1) per platform
WORDING = {
    "linux": ("Start DragShift when you log in", "Start when you log in", "in Files or on the desktop"),
    "darwin": ("Open DragShift at login", "Open at login", "in Finder or on the desktop"),
    "win32": ("Start DragShift with Windows", "Start with Windows", "in File Explorer or on the desktop"),
}


class FakeSettings(settings.Settings):
    def __init__(self):
        self.values = dict(settings.DEFAULTS)

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def calls(monkeypatch):
    """Record set_autostart instead of touching the desktop, registry or LaunchAgents."""
    seen = []
    monkeypatch.setattr(settings, "autostart_enabled", lambda: False)
    monkeypatch.setattr(settings, "set_autostart", seen.append)
    return seen


def as_platform(monkeypatch, plat):
    monkeypatch.setattr(settings, "IS_WIN", plat == "win32")
    monkeypatch.setattr(settings, "IS_MAC", plat == "darwin")
    monkeypatch.setattr(settings, "IS_LINUX", plat == "linux")
    monkeypatch.setattr(platform_mac, "has_screen_recording", lambda: True)


def texts(w, kind=QLabel):
    return [c.text() for c in w.findChildren(kind)]


@pytest.mark.parametrize("plat", WORDING)
def test_wording_per_platform(qapp, calls, monkeypatch, plat):
    as_platform(monkeypatch, plat)
    s_box, w_box, step1 = WORDING[plat]
    sw = settings.SettingsWindow(FakeSettings())
    ww = settings.WelcomeWindow(FakeSettings())
    assert sw.auto.text() == s_box
    assert ww.auto.text() == w_box
    assert step1 in texts(ww)
    sw.close(), ww.close()


@pytest.fixture
def linux(monkeypatch):
    as_platform(monkeypatch, "linux")
    # make sure nothing on the Linux path consults the mac permission check
    monkeypatch.setattr(platform_mac, "has_screen_recording",
                        lambda: pytest.fail("Screen Recording checked on Linux"))


def test_linux_has_no_macos_only_controls(qapp, calls, linux):
    sw = settings.SettingsWindow(FakeSettings())
    ww = settings.WelcomeWindow(FakeSettings())
    for w in (sw, ww):
        shown = " ".join(texts(w) + texts(w, QPushButton) + texts(w, QCheckBox))
        assert "Screen Recording" not in shown and "liquid glass" not in shown
        assert "Finder" not in shown and "Explorer" not in shown and "Windows" not in shown
    assert [b.text() for b in sw.findChildren(QPushButton) if b.text() != "✕"] == ["Done"]
    assert [b.text() for b in ww.findChildren(QPushButton) if b.text() != "✕"] == ["Got it"]
    sw.close(), ww.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="TRIGGERS is built at import from the real platform")
def test_linux_trigger_labels(qapp, calls, linux):
    sw = settings.SettingsWindow(FakeSettings())
    assert [sw.trigger.itemText(i) for i in range(sw.trigger.count())] == ["Shift", "Ctrl", "Alt"]
    ww = settings.WelcomeWindow(FakeSettings(), dict(settings.TRIGGERS)["shift"])
    assert "Press Shift" in texts(ww) and "Tap Shift again" in texts(ww)
    sw.close(), ww.close()


@pytest.mark.parametrize("checked", [True, False])
def test_welcome_done_sets_autostart_on_linux(qapp, calls, linux, checked):
    store = FakeSettings()
    ww = settings.WelcomeWindow(store)
    assert ww.auto.isChecked() is True                 # default on
    ww.auto.setChecked(checked)
    ww._done()
    assert calls == [checked]
    assert store.values["welcomed"] is True
