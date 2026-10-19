"""demo/e2e_linux.py, the Linux E2E runner (#57), end to end: it boots its own isolated
GNOME session, starts DragShift, Shift+drags Photo.jpg from Files onto the PNG petal and
writes the same artefacts as the Windows runner."""
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
    assert "JPG → PNG" in r.stdout.splitlines()
    r = _runner("--only", "no such scenario", timeout=60)
    assert r.returncode == 2 and "unknown scenario" in r.stderr


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
