"""Linux: a start that no running instance takes over asks the shell extension to start
the app as its own Wayland client (#95, `handoff.start_managed`), then hands this launch's
files to it. The extension side runs in tests/linux/test_dock_hide.py; here the D-Bus call
and the IPC socket are faked."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux start-up")

EXT = Path(__file__).resolve().parent.parent / "extension" / "dragshift@eteen12.github.io"


@pytest.fixture
def fake(monkeypatch):
    """launch_app() and the IPC send, recorded. `answer_after` sends fail before the new
    instance listens."""
    from dragshift import handoff, platform_linux
    state = {"launch": [], "sent": [], "launch_ok": True, "answer_after": 2}

    def launch_app(quiet):
        state["launch"].append(quiet)
        return state["launch_ok"]

    def send(payload):
        if state["answer_after"] > 0:
            state["answer_after"] -= 1
            return False
        state["sent"].append(payload)
        return True

    monkeypatch.setattr(platform_linux, "launch_app", launch_app)
    monkeypatch.setattr(handoff, "_send", send)
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    monkeypatch.delenv(platform_linux.MANAGED_ENV, raising=False)
    return state


def test_a_plain_start_is_handed_to_the_managed_app(fake):
    from dragshift.handoff import start_managed
    assert start_managed(["dragshift"]) is True
    assert fake["launch"] == [False]                # not quiet: it shows its own toast
    assert fake["sent"] == [{"show": False}]        # just waits until it listens


def test_files_go_to_the_managed_app(fake, tmp_path):
    from dragshift.handoff import start_managed
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    assert start_managed(["dragshift", str(photo), str(tmp_path / "missing.jpg")]) is True
    assert fake["launch"] == [True]                 # quiet: the menu opens instead
    assert fake["sent"] == [{"files": [str(photo)]}]


def test_quiet_start(fake):
    from dragshift.handoff import start_managed
    assert start_managed(["dragshift", "--quiet"]) is True
    assert fake["launch"] == [True]


def test_without_the_extension_or_launcher_the_app_runs_here(fake):
    from dragshift.handoff import start_managed
    fake["launch_ok"] = False
    assert start_managed(["dragshift"]) is False
    assert fake["sent"] == []


def test_the_managed_app_itself_never_asks_again(fake, monkeypatch):
    import os

    from dragshift import platform_linux
    from dragshift.handoff import start_managed
    monkeypatch.setenv(platform_linux.MANAGED_ENV, "1")
    assert start_managed(["dragshift", "--quiet"]) is False
    assert fake["launch"] == []
    assert platform_linux.MANAGED_ENV not in os.environ   # not inherited by its children


def test_selftest_never_asks(fake):
    from dragshift.handoff import start_managed
    assert start_managed(["dragshift", "--selftest"]) is False
    assert fake["launch"] == []


def test_runs_here_when_the_started_app_never_answers(fake, monkeypatch, capsys):
    from dragshift import handoff
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(handoff.time, "monotonic", lambda: next(clock))
    fake["answer_after"] = 10 ** 6
    assert handoff.start_managed(["dragshift"]) is False
    assert "did not answer" in capsys.readouterr().err


def test_launch_without_a_session_bus_is_false():
    # tests/conftest.py points the session bus at a socket that does not exist
    from dragshift import platform_linux
    assert platform_linux.launch_app(True) is False


def test_launch_interface_matches_the_client(monkeypatch):
    from dragshift import platform_linux
    (iface,) = ET.parse(EXT / "dbus.xml").getroot().findall("interface")
    m = iface.find("method[@name='Launch']")
    assert [(a.get("type"), a.get("direction")) for a in m.findall("arg")] == \
        [("b", "in"), ("b", "out")]
    assert "LaunchAsync([quiet], invocation)" in (EXT / "extension.js").read_text()
    assert f"'{platform_linux.MANAGED_ENV}'" in (EXT / "launcher.js").read_text()
    calls = []
    monkeypatch.setattr(platform_linux, "_call",
                        lambda *a, **k: calls.append(a) or (True,))
    assert platform_linux.launch_app(False) is True
    assert calls == [("Launch", "b", False)]


def test_main_module_hands_off_before_asking_the_extension():
    src = (Path(__file__).resolve().parent.parent / "dragshift" / "__main__.py").read_text()
    assert src.index("try_handoff(sys.argv)") < src.index("start_managed(sys.argv)") < \
        src.index("from dragshift.app import main")
