"""The shipped GNOME Shell extension dragshift@eteen12.github.io (extension/) and its
Python client in dragshift/platform_linux.py.

Session tests load the extension only inside the isolated session from
tests/linux/session.py, never on the real desktop.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from dragshift import __version__, platform_linux as lx
from tests.linux.conftest import require_session_tools
from tests.linux.session import Session

ROOT = Path(__file__).resolve().parents[2]
UUID = "dragshift@eteen12.github.io"
SRC = ROOT / "extension" / UUID
BUILD = ROOT / "extension" / "build.sh"
ZIP_NAME = f"{UUID}.shell-extension.zip"

gnome = pytest.mark.gnome


def _build(out: Path, env=None) -> Path:
    r = subprocess.run(["sh", str(BUILD), str(out)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    zp = out / ZIP_NAME
    assert r.stdout.strip() == str(zp)
    return zp


@pytest.fixture(scope="module")
def built_zip(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("dist"))


@pytest.fixture(scope="module")
def built_dir(built_zip, tmp_path_factory) -> Path:
    """The built zip unpacked into a directory named by the UUID (what the shell loads)."""
    d = tmp_path_factory.mktemp("ext") / UUID
    with zipfile.ZipFile(built_zip) as z:
        z.extractall(d)
    return d


# ------------------------------------------------------------------ build, no shell
def test_build_zip_contents(built_zip):
    with zipfile.ZipFile(built_zip) as z:
        assert sorted(z.namelist()) == ["caller.js", "dbus.xml", "extension.js",
                                        "metadata.json", "placement.js"]
        meta = json.loads(z.read("metadata.json"))
        for name in ("extension.js", "caller.js", "placement.js", "dbus.xml"):
            assert z.read(name) == (SRC / name).read_bytes(), name
    assert meta == {**json.loads((SRC / "metadata.json").read_text()),
                    "version-name": __version__}
    assert meta["uuid"] == UUID and meta["name"] == "DragShift"
    assert meta["shell-version"] == ["46"]          # this project's only target
    assert meta["url"] == "https://github.com/eteen12/DragShift"
    assert meta["version"] == 1


def test_source_metadata_has_no_version_name():
    """The version comes from dragshift/__init__.py at build time, never a second copy."""
    assert "version-name" not in json.loads((SRC / "metadata.json").read_text())


def test_build_default_out_dir_is_repo_dist():
    r = subprocess.run(["sh", str(BUILD)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(ROOT / "dist" / ZIP_NAME)
    assert (ROOT / "dist" / ZIP_NAME).is_file()


def test_build_without_gnome_extensions_tool(tmp_path, built_zip):
    """CI and Docker images may lack gnome-extensions: Python's zipfile packs the same."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("sh", "dirname", "mkdir", "mktemp", "cp", "rm", "python3"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    out = tmp_path / "out"
    zp = _build(out, env={"PATH": str(bin_dir)})
    with zipfile.ZipFile(zp) as a, zipfile.ZipFile(built_zip) as b:
        assert sorted(a.namelist()) == sorted(b.namelist())
        for name in a.namelist():
            assert a.read(name) == b.read(name), name


def test_dbus_xml_matches_python_client():
    node = ET.parse(SRC / "dbus.xml").getroot()
    (iface,) = node.findall("interface")
    assert iface.get("name") == lx.INTERFACE
    version = iface.find("method[@name='Version']")
    assert [(a.get("type"), a.get("direction")) for a in version.findall("arg")] == [("s", "out")]
    pointer = iface.find("method[@name='GetPointer']")
    assert [(a.get("name"), a.get("type"), a.get("direction")) for a in pointer.findall("arg")] \
        == [("x", "i", "out"), ("y", "i", "out"), ("mask", "u", "out")]
    place = iface.find("method[@name='PlaceWindow']")
    assert [(a.get("name"), a.get("type"), a.get("direction")) for a in place.findall("arg")] \
        == [("title", "s", "in"), ("x", "i", "in"), ("y", "i", "in"), ("placed", "b", "out")]
    js = (SRC / "extension.js").read_text()
    assert f"BUS_NAME = '{lx.BUS_NAME}'" in js
    assert f"OBJECT_PATH = '{lx.OBJECT_PATH}'" in js
    assert lx.EXTENSION_UUID == UUID


# ------------------------------------------------------------------ client, no shell
def _client(code: str, env: dict) -> subprocess.CompletedProcess:
    """Run Python with the repo on the path in a clean process (its own D-Bus connection)."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=30, env={**env, "PYTHONPATH": str(ROOT)})


_TIMED_VERSION = ("import time; from dragshift import platform_linux as p; "
                  "t = time.monotonic(); v = p.extension_version(); "
                  "print(repr(v), round((time.monotonic() - t) * 1000))")


def test_client_without_a_session_bus(tmp_path):
    r = _client(_TIMED_VERSION, {"PATH": os.environ["PATH"],
                                 "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/none"})
    assert r.returncode == 0, r.stderr
    v, ms = r.stdout.split()
    assert v == "None" and int(ms) < 500


@pytest.mark.skipif(not shutil.which("dbus-run-session"), reason="needs dbus-run-session")
def test_client_on_a_bus_without_the_extension_and_with_a_hung_one(tmp_path):
    """No owner → None at once; an owner that never answers → None after the timeout."""
    script = tmp_path / "hung.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection
        from dragshift import platform_linux as p

        def version():
            t = time.monotonic()
            v = p.extension_version()
            return v, (time.monotonic() - t) * 1000

        print("absent", *version())
        conn = open_dbus_connection(bus="SESSION")   # owns the name, never replies
        conn.send_and_get_reply(message_bus.RequestName({lx.BUS_NAME!r}))
        print("hung", *version())
        print("hung-again", *version())
    """))
    r = subprocess.run(["dbus-run-session", "--", sys.executable, str(script)],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                            "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    rows = {line.split()[0]: line.split()[1:] for line in r.stdout.splitlines()}
    assert rows["absent"][0] == "None" and float(rows["absent"][1]) < 150
    for key in ("hung", "hung-again"):
        v, ms = rows[key]
        assert v == "None" and 150 < float(ms) < 600, (key, ms)


def test_app_warns_on_version_mismatch(monkeypatch, capsys):
    from dragshift import app
    refused = "the GNOME Shell extension only answers the DragShift app"
    for found, refuses, expect in (
            ("0.9.0", False, "warning: the GNOME Shell extension is from DragShift "
                             f"0.9.0, the app is {__version__}"),
            (None, True, "GNOME Shell extension not available"),
            (__version__, False, ""),
            (__version__, True, refused)):     # #93: e.g. started as `python run.py`
        monkeypatch.setattr(lx, "extension_version", lambda v=found: v)
        monkeypatch.setattr(lx, "extension_refuses_us", lambda r=refuses: r)
        app.check_extension_version().join(5)
        err = capsys.readouterr().err
        assert (expect in err) if expect else err == "", (found, err)
        assert (refused in err) == (refuses and found is not None), err


# ------------------------------------------------------------------ in the session
def _has_owner(s, name=lx.BUS_NAME) -> bool:
    r = s.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
               "--object-path", "/org/freedesktop/DBus",
               "--method", "org.freedesktop.DBus.NameHasOwner", name], check=True)
    return r.stdout.strip() == "(true,)"


def _gdbus_version(s):
    r = s.run(["gdbus", "call", "--session", "--timeout", "2", "--dest", lx.BUS_NAME,
               "--object-path", lx.OBJECT_PATH, "--method", f"{lx.INTERFACE}.Version"])
    m = re.fullmatch(r"\('([^']*)',\)", r.stdout.strip())
    return m.group(1) if r.returncode == 0 and m else None


def _client_version(s):
    r = s.run([sys.executable, "-c", "from dragshift import platform_linux as p; "
               "print(p.extension_version())"], env={"PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _state(s, uuid=UUID):
    r = s.run(["gnome-extensions", "info", uuid])
    m = re.search(r"^\s*State: (.+)$", r.stdout, re.M)
    return m.group(1).strip() if m else None


def _wait(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    return fn()


def _errors_about(log: str, uuid: str = UUID) -> list[str]:
    """Shell log lines that report an error from `uuid`: JS ERROR blocks whose stack names
    it, and any line that names it together with "error"."""
    lines = log.splitlines()
    bad = []
    for i, line in enumerate(lines):
        if "JS ERROR" in line:
            block = lines[i:i + 12]
            if any(uuid in b for b in block):
                bad.extend(block)
        elif uuid in line and "error" in line.lower():
            bad.append(line)
    return bad


@pytest.fixture(scope="module")
def ext_session(built_dir):
    require_session_tools()
    with Session(extensions=[built_dir]) as s:
        yield s


@gnome
def test_zip_installs_with_gnome_extensions(gnome_session, built_zip):
    s = gnome_session
    r = s.run(["gnome-extensions", "install", "--force", str(built_zip)])
    assert r.returncode == 0, r.stderr
    installed = Path(s.env()["XDG_DATA_HOME"]) / "gnome-shell" / "extensions" / UUID
    assert json.loads((installed / "metadata.json").read_text())["version-name"] == __version__
    assert (installed / "dbus.xml").is_file() and (installed / "extension.js").is_file()
    # again over the existing copy (update in place)
    assert s.run(["gnome-extensions", "install", "--force", str(built_zip)]).returncode == 0


@gnome
def test_extension_is_active_and_answers_version(ext_session):
    s = ext_session
    assert _wait(lambda: _state(s) == "ACTIVE"), _state(s)
    assert _wait(lambda: _has_owner(s))
    assert _gdbus_version(s) == __version__
    assert _client_version(s) == __version__
    r = s.run(["gdbus", "introspect", "--session", "--dest", lx.BUS_NAME,
               "--object-path", lx.OBJECT_PATH])
    assert f"interface {lx.INTERFACE} {{" in r.stdout and "Version(out s version);" in r.stdout
    assert "GetPointer(out i x,\n" in r.stdout and "out u mask);" in r.stdout


@gnome
def test_disable_releases_the_name_and_enable_brings_it_back(ext_session):
    s = ext_session
    assert _wait(lambda: _has_owner(s))
    for _ in range(2):
        assert s.run(["gnome-extensions", "disable", UUID]).returncode == 0
        assert _wait(lambda: _state(s) == "INACTIVE"), _state(s)
        assert _wait(lambda: not _has_owner(s))
        assert _gdbus_version(s) is None
        assert _client_version(s) == "None"
        assert s.run(["gnome-extensions", "enable", UUID]).returncode == 0
        assert _wait(lambda: _state(s) == "ACTIVE"), _state(s)
        assert _wait(lambda: _has_owner(s))
        assert _gdbus_version(s) == __version__


@gnome
def test_no_errors_from_the_extension_in_the_shell_log(ext_session):
    s = ext_session
    assert _wait(lambda: _state(s) == "ACTIVE")
    log = s.log_path.read_text(errors="replace")
    assert _errors_about(log) == []


# A test-only extension that calls DragShift's enable()/disable() directly, because the
# shell itself never calls them twice in a row.
PROBE_UUID = "dragshift-probe@dragshift.test"
PROBE_BUS = "io.github.eteen12.DragShiftProbe"
PROBE_JS = """
import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const XML = `<node><interface name="%(bus)s">
  <method name="Call"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;

export default class Probe {
    enable() {
        this._obj = Gio.DBusExportedObject.wrapJSObject(XML, {
            Call(method) {
                try {
                    Main.extensionManager.lookup('%(uuid)s').stateObj[method]();
                    return '';
                } catch (e) {
                    return String(e);
                }
            },
        });
        this._obj.export(Gio.DBus.session, '/probe');
        this._id = Gio.bus_own_name(Gio.BusType.SESSION, '%(bus)s', 0, null, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._id);
        this._obj.unexport();
        this._obj = null;
    }
}
""" % {"bus": PROBE_BUS, "uuid": UUID}


def _probe(s, method):
    r = s.run(["gdbus", "call", "--session", "--dest", PROBE_BUS, "--object-path", "/probe",
               "--method", f"{PROBE_BUS}.Call", method], check=True)
    return r.stdout.strip()


@gnome
def test_enable_and_disable_are_idempotent_and_wrong_shell_version_is_not_loaded(
        built_dir, tmp_path):
    require_session_tools()
    probe = tmp_path / PROBE_UUID
    probe.mkdir()
    (probe / "metadata.json").write_text(json.dumps(
        {"uuid": PROBE_UUID, "name": "Probe", "description": "test", "shell-version": ["46"]}))
    (probe / "extension.js").write_text(PROBE_JS)
    # The same extension pinned to another shell version must not load at all.
    old_uuid = "dragshift-outdated@dragshift.test"
    old = tmp_path / old_uuid
    shutil.copytree(built_dir, old)
    meta = json.loads((old / "metadata.json").read_text())
    (old / "metadata.json").write_text(json.dumps({**meta, "uuid": old_uuid,
                                                   "shell-version": ["45"]}))
    with Session(extensions=[built_dir, probe, old]) as s:
        assert _wait(lambda: _state(s) == "ACTIVE")
        assert _wait(lambda: _has_owner(s, PROBE_BUS)) and _wait(lambda: _has_owner(s))
        assert _state(s, old_uuid) == "OUT OF DATE"

        ok = "('',)"
        assert _probe(s, "enable") == ok                 # enabled twice
        assert _gdbus_version(s) == __version__
        assert _probe(s, "disable") == ok                # one disable undoes it all
        assert _wait(lambda: not _has_owner(s)), "a second name ownership leaked"
        assert _gdbus_version(s) is None
        assert _probe(s, "disable") == ok                # disabled twice
        assert _probe(s, "enable") == ok
        assert _wait(lambda: _has_owner(s))
        assert _gdbus_version(s) == __version__
        log = s.log_path.read_text(errors="replace")
        assert _errors_about(log) == []
        assert _errors_about(log, old_uuid) == []
