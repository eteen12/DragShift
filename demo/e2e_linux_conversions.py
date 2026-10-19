"""Every conversion scenario of demo/e2e_windows.py::main, from Files on Linux (#60).

`JPG → PNG` stays in e2e_linux.py; these are the rest of the Convert ring, with the same
names and verifiers as on Windows. Two wrappers add Linux checks without changing the shared
verifiers: HEIC/AVIF must really decode as HEIF/AVIF, and DOCX → PDF records which renderer
made the PDF (LibreOffice, or the built-in one the app falls back to, e.g. in the isolated
session where the snap can't start, #53) and accepts either."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from e2e_common import v_combined, v_format, v_pages_to_images
from e2e_linux import Scenario, app_text

EXT = {"TIFF": ".tiff", "HEIC": ".heic"}
CODEC = {"HEIC": "HEIF", "AVIF": "AVIF"}   # Pillow's format name for the decoded file
FALLBACK = "docx->pdf: LibreOffice "        # "… failed (why), using …" / "… not found, using …"


def v_codec(fmt: str, ext: str):
    """v_format (which opens HEIC/AVIF with any decoder, as on Windows), and the file really
    is HEIF/AVIF, not a JPEG with another extension."""
    base = v_format(fmt, ext)

    def v(outs, srcs):
        note = base(outs, srcs)
        with Image.open(outs[0]) as im:
            assert im.format == CODEC[fmt], f"{outs[0].name}: decodes as {im.format}"
        return f"{note}, {CODEC[fmt]}"
    return v


def docx_renderer(pdf: Path, gesture: str) -> str:
    """Which renderer made `pdf`: the app's log line after this drop names the fallback, and
    the PDF's producer has to agree with it (LibreOffice writes "LibreOffice <version>", the
    built-in renderer no producer at all)."""
    import pymupdf
    with pymupdf.open(pdf) as d:
        producer = (d.metadata or {}).get("producer") or ""
    fallback = [ln for ln in gesture.splitlines() if ln.startswith(FALLBACK)]
    if fallback:
        assert "LibreOffice" not in producer, f"fallback logged, but producer {producer!r}"
        m = re.match(r"(not found|failed \(([^:)]*))", fallback[-1][len(FALLBACK):])
        why = (f"failed: {m[2]}" if m[2] else m[1]) if m else "failed"
        return f"built-in renderer (LibreOffice {why})"   # the whole line is in app_log.txt
    assert "LibreOffice" in producer, f"no fallback logged, but producer {producer!r}"
    return producer


def v_docx_pdf(outs, srcs):
    """v_format("PDF"), and which renderer ran; both pass (#53)."""
    note = v_format("PDF", ".pdf")(outs, srcs)
    text = app_text()
    gesture = text[text.rfind("menu: dropped "):]   # this scenario's drop: run() checked it
    return f"{note} · {docx_renderer(outs[0], gesture)}"


SCENARIOS: list[Scenario] = [
    *[Scenario(f"JPG → {fmt}", "nautilus", ["Photo.jpg"], "Convert", fmt,
               (v_codec if fmt in CODEC else v_format)(fmt, EXT.get(fmt, "." + fmt.lower())))
      for fmt in ["WEBP", "HEIC", "TIFF", "AVIF", "BMP", "PDF", "ICO", "GIF"]],
    Scenario("PNG → JPG", "nautilus", ["Graphic.png"], "Convert", "JPG", v_format("JPEG", ".jpg")),
    Scenario("PDF → JPG (3 pages)", "nautilus", ["Report.pdf"], "Convert", "JPG",
             v_pages_to_images(3, ".jpg")),
    Scenario("PDF → PNG (3 pages)", "nautilus", ["Report.pdf"], "Convert", "PNG",
             v_pages_to_images(3, ".png")),
    Scenario("PDF → DOCX", "nautilus", ["Report.pdf"], "Convert", "DOCX", v_format("DOCX", ".docx")),
    Scenario("PDF → TXT", "nautilus", ["Report.pdf"], "Convert", "TXT", v_format("TXT", ".txt")),
    Scenario("DOCX → PDF", "nautilus", ["Letter.docx"], "Convert", "PDF", v_docx_pdf),
    Scenario("DOCX → TXT", "nautilus", ["Letter.docx"], "Convert", "TXT", v_format("TXT", ".txt")),
    Scenario("TXT → PDF", "nautilus", ["Notes.txt"], "Convert", "PDF", v_format("PDF", ".pdf")),
    Scenario("TXT → DOCX", "nautilus", ["Notes.txt"], "Convert", "DOCX", v_format("DOCX", ".docx")),
    Scenario("2 images → 1 PDF", "nautilus", ["Photo.jpg", "Second.jpg"], "Convert", "PDF",
             v_combined, select_extra=True),
]
