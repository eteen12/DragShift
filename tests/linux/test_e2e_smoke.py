"""demo/e2e_linux.py, the Linux E2E runner (#57), end to end: it boots its own isolated
GNOME session, starts DragShift, Shift+drags Photo.jpg from Files onto the PNG petal and
writes the same artefacts as the Windows runner. Scenarios also come from
demo/e2e_linux_*.py modules (`collect`)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.linux.conftest import require_session_tools

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "demo" / "e2e_linux.py"


def _runner(*args, timeout=300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUNNER), *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout)


def test_list_and_unknown_names():
    r = _runner("--list", timeout=60)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[:2] == ["e2e_linux.py", "  JPG → PNG"], lines
    r = _runner("--only", "no such scenario", timeout=60)
    assert r.returncode == 2 and "unknown scenario" in r.stderr


MODULE = """from e2e_linux import Scenario
from e2e_common import v_format
SCENARIOS = [Scenario(n, "nautilus", ["Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png"))
             for n in {names}]
"""


@pytest.fixture
def e2e_linux(monkeypatch):
    monkeypatch.syspath_prepend(str(RUNNER.parent))
    import e2e_linux
    yield e2e_linux
    for name in [m for m in sys.modules if m.startswith("e2e_linux_")]:
        del sys.modules[name]


def _module(folder: Path, stem: str, names=None, text=None) -> None:
    (folder / f"{stem}.py").write_text(text if text is not None else
                                       MODULE.replace("{names}", repr(names)), encoding="utf-8")


def test_scenarios_are_collected_from_e2e_linux_modules_in_name_order(e2e_linux, tmp_path):
    _module(tmp_path, "e2e_linux_pdf", ["PDF one", "PDF two"])
    _module(tmp_path, "e2e_linux_conversions", ["JPG → WEBP"])
    (tmp_path / "e2e_linux.py").write_text("raise SystemExit('not a scenario module')")
    (tmp_path / "other.py").write_text("raise SystemExit('not a scenario module')")
    groups = e2e_linux.collect(tmp_path)
    assert [(f, [x.name for x in s]) for f, s in groups] == [
        ("e2e_linux.py", ["JPG → PNG"]),
        ("e2e_linux_conversions.py", ["JPG → WEBP"]),
        ("e2e_linux_pdf.py", ["PDF one", "PDF two"])]
    assert all(isinstance(x, e2e_linux.Scenario) for _, s in groups for x in s)


@pytest.mark.parametrize("text,why", [
    ("import no_such_module_anywhere\n", "failed to import"),
    ("1 / 0\n", "ZeroDivisionError"),
    ("SCENARIOS = None\n", "no SCENARIOS list"),
    ("X = 1\n", "no SCENARIOS list"),
])
def test_a_broken_scenario_module_stops_the_run(e2e_linux, tmp_path, text, why):
    _module(tmp_path, "e2e_linux_ok", ["Fine"])
    _module(tmp_path, "e2e_linux_zz_bad", text=text)
    with pytest.raises(e2e_linux.ScenarioModuleError, match=why):
        e2e_linux.collect(tmp_path)


def test_a_repeated_scenario_name_stops_the_run(e2e_linux, tmp_path):
    _module(tmp_path, "e2e_linux_a", ["JPG → PNG"])
    with pytest.raises(e2e_linux.ScenarioModuleError, match="in e2e_linux.py and e2e_linux_a.py"):
        e2e_linux.collect(tmp_path)


@pytest.mark.gnome
def test_jpg_to_png_passes(tmp_path):
    require_session_tools()
    r = _runner("--only", "JPG → PNG", "--out", str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert [(x["name"], x["where"], x["status"]) for x in results] == \
        [("JPG → PNG", "nautilus", "PASS")], results
    assert "Photo.png · 2400×1600" in results[0]["detail"]
    md = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert md.startswith("# DragShift Linux end-to-end test — 1/1 passed")
    assert "| 1 | nautilus | JPG → PNG | ✅ |" in md
    assert (tmp_path / "shots" / "01_JPG___PNG_menu.png").stat().st_size > 10_000
    app_log = (tmp_path / "app_log.txt").read_text(encoding="utf-8")
    assert "menu: dropped 1 file(s) on PNG (Convert)" in app_log
    assert "DONE 1/1 passed" in (tmp_path / "log.txt").read_text(encoding="utf-8")
