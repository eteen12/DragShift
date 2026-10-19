"""demo/e2e_common.py holds what the Windows, macOS and Linux E2E runners share (#57).
The Windows and macOS runners can't run here (pyautogui, UI Automation, Quartz), so this
checks the move statically: every name e2e_common defines is imported back into
e2e_windows.py under the same name, nothing is defined twice, the macOS runner's imports
exist, and e2e_common itself loads without any desktop-driving module."""
import ast
import os
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "demo"


def _tree(name: str) -> ast.Module:
    return ast.parse((DEMO / name).read_text(encoding="utf-8"))


def _defined(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _imported_from(tree: ast.Module, module: str) -> set[str]:
    return {a.asname or a.name for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == module for a in node.names}


SHARED = _defined(_tree("e2e_common.py")) - {"ROOT"}


def test_windows_runner_imports_every_shared_name_back():
    win = _tree("e2e_windows.py")
    assert SHARED - _imported_from(win, "e2e_common") == set()
    assert SHARED & _defined(win) == set()          # moved, not copied


def test_the_names_the_windows_runner_had_are_all_there():
    win = _defined(_tree("e2e_windows.py")) | _imported_from(_tree("e2e_windows.py"), "e2e_common")
    for name in ("make_photo", "make_pdf", "make_files", "snapshot", "new_files", "img_ok",
                 "pdf_pages", "SOURCES", "ring_index", "safe", "stash", "restore_sources", "run",
                 "v_format", "v_pages_to_images", "v_compress", "v_dims", "v_exists", "v_redact",
                 "v_no_gps_keep_camera", "v_no_meta", "v_pdf_n", "v_merged", "v_rotated",
                 "v_protected", "v_pdf_meta", "v_pdf_compress", "v_extract", "v_combined"):
        assert name in win, name


def test_mac_runner_imports_exist_in_common():
    mac = _tree("e2e_mac.py")
    wanted = _imported_from(mac, "e2e_common")
    assert wanted and wanted <= SHARED
    assert not _imported_from(mac, "e2e_windows")


def test_common_loads_without_desktop_drivers(tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import e2e_common as c; "
            "bad = {'pyautogui', 'winkit', 'Quartz', 'pywinauto'} & set(sys.modules); "
            "assert not bad, bad; "
            "from pathlib import Path; d = Path(sys.argv[2]) / 'f'; c.make_files(d); "
            "assert sorted(c.snapshot(d)) == sorted(c.SOURCES), c.snapshot(d); "
            "assert c.img_ok(d / 'Photo.jpg', 'JPEG') == (2400, 1600); "
            "assert c.pdf_pages(d / 'Report.pdf') == 3 and c.pdf_pages(d / 'Appendix.pdf') == 2; "
            "print(c.v_format('PNG', '.png')([d / 'Graphic.png'], []))")
    r = subprocess.run([sys.executable, "-c", code, str(DEMO), str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Graphic.png · 800×533"
