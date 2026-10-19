"""demo/e2e_linux_conversions.py (#60) without a session: it lists every conversion of
demo/e2e_windows.py::main under the same name, and its DOCX → PDF check tells the two
renderers apart. The real drags run in demo/e2e_linux.py."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"


@pytest.fixture
def conversions(monkeypatch):
    monkeypatch.syspath_prepend(str(DEMO))
    import e2e_linux  # noqa: F401  (the module imports it, as the runner registers it)
    import e2e_linux_conversions
    yield e2e_linux_conversions
    sys.modules.pop("e2e_linux_conversions", None)


def windows_conversions() -> list[str]:
    """The Convert scenarios in e2e_windows.py's main (read as text: it needs Windows)."""
    src = (DEMO / "e2e_windows.py").read_text(encoding="utf-8")
    fmts = re.search(r'for fmt in \[([^\]]*)\]:\s*\n[^\n]*\n\s*run\(f"JPG → \{fmt\}"', src)[1]
    names = [f"JPG → {f}" for f in re.findall(r'"(\w+)"', fmts)]
    return names + re.findall(r'run\("([^"]+)", "explorer", \[[^\]]*\], "Convert"', src)


def test_every_windows_conversion_runs_on_linux(conversions):
    import e2e_linux
    ours = [x.name for x in e2e_linux.SCENARIOS + conversions.SCENARIOS]
    assert len(windows_conversions()) == 19
    assert sorted(ours) == sorted(windows_conversions())
    assert all(x.ring == "Convert" and x.where == "nautilus" for x in conversions.SCENARIOS)
    multi = [x for x in conversions.SCENARIOS if x.select_extra]
    assert [x.name for x in multi] == ["2 images → 1 PDF"]


def _pdf(path: Path, producer: str) -> Path:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    doc.set_metadata({"producer": producer})
    doc.save(path)
    return path


FAILED = ("docx->pdf: LibreOffice failed (exit 1, no PDF: /user.slice/app.scope is not a snap "
          "cgroup for tag snap.libreoffice.libreoffice), using the built-in renderer")
DROP = "menu: dropped 1 file(s) on PDF (Convert), proposed CopyAction, answered CopyAction"


def test_docx_renderer_names_the_one_that_ran(conversions, tmp_path):
    lo = _pdf(tmp_path / "lo.pdf", "LibreOffice 26.2.5.2 (X86_64)")
    builtin = _pdf(tmp_path / "builtin.pdf", "")
    r = conversions.docx_renderer
    assert r(lo, DROP) == "LibreOffice 26.2.5.2 (X86_64)"
    assert r(builtin, f"{DROP}\n{FAILED}") == "built-in renderer (LibreOffice failed: exit 1, no PDF)"
    assert r(builtin, f"{DROP}\ndocx->pdf: LibreOffice not found, using the built-in renderer") \
        == "built-in renderer (LibreOffice not found)"
    with pytest.raises(AssertionError, match="no fallback logged"):
        r(builtin, DROP)
    with pytest.raises(AssertionError, match="fallback logged"):
        r(lo, f"{DROP}\n{FAILED}")
