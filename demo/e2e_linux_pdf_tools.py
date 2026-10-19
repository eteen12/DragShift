"""PDF tool scenarios for demo/e2e_linux.py (#62): the twins of e2e_windows.py's "PDF tools"
block. Every one is a Shift+drag of a PDF in Files onto the Tools ring.

Compress, Split, Rotate, Extract and Merge run straight from the drop. Protect and Metadata
open a glass tool window, driven here with keyboard input injected through the test helper:
the driver finds the window by title and app id (never by pid, #95), activates it and types
(the password field has the focus; `ToolWindow._enter` makes Enter press the primary
button). #132 moves these two drivers onto #61's shared `tool_window()` once that lands.

Each scenario runs the shared verifier of the Windows runner (e2e_common), then a stricter
Linux check on the same output (`_and`)."""
from __future__ import annotations

import time
from pathlib import Path

from e2e_common import (pdf_pages, safe, v_extract, v_merged, v_pdf_compress, v_pdf_meta,
                        v_pdf_n, v_protected, v_rotated)
from e2e_linux import RESULTS, C, Scenario, inject, log, shot, wait

from dragshift.app import LINUX_APP_ID

PASSWORD = "keywest2026"      # the Windows runner's password (v_protected checks it)
WINDOW_WAIT = 10.0            # s for a tool window after the drop


# ------------------------------------------------------------------ tool windows
def tool_windows(title: str) -> list[dict]:
    """DragShift's glass windows called `title` (GlassWindow adds " — DragShift", and
    " (2)" when one is already open)."""
    return [w for w in C.shell.list_windows()
            if w["wm_class"] == LINUX_APP_ID and w["title"].startswith(title)
            and w["title"].endswith("— DragShift")]


def close_tool_windows(title: str):
    """Escape in every window still open (GlassWindow closes on Escape)."""
    for w in tool_windows(title):
        C.shell.activate_window(w["id"])
        time.sleep(0.2)
        inject.press(C.shell, "Escape")
    if not wait(lambda: not tool_windows(title), 3, 0.1):
        log(f"{title!r} windows still open:", [w["title"] for w in tool_windows(title)])


def drive(title: str, keys):
    """Wait for the tool window, focus it, send `keys(window)`, then wait for it to close
    (the tool saves in the background and closes the window). On failure, close it so the
    next scenario starts clean."""
    try:
        win = wait(lambda: tool_windows(title), WINDOW_WAIT, 0.1)
        if not win:
            raise AssertionError(f"no {title!r} window within {WINDOW_WAIT:.0f} s")
        win = win[-1]
        time.sleep(0.4)                       # shown, glass backdrop captured
        C.shell.activate_window(win["id"])
        if not wait(lambda: any(w["focused"] for w in tool_windows(title)), 3, 0.05):
            raise AssertionError(f"the {title!r} window did not take the focus")
        time.sleep(0.2)
        keys(win)
        if not wait(lambda: not tool_windows(title), 5, 0.1):
            raise AssertionError(f"the {title!r} window stayed open")
    except BaseException:
        close_tool_windows(title)
        raise


def tool_shot(win_title: str):
    shot(f"{len(RESULTS) + 1:02d}_{safe(win_title)}_tool")


def drive_protect():
    """Password, Tab, the same in Confirm, then Enter (Protect)."""
    def keys(win):
        inject.type_text(C.shell, PASSWORD + "\t" + PASSWORD)
        time.sleep(0.3)
        tool_shot("PDF Protect")
        inject.press(C.shell, "Return")
    drive("Password-protect PDF", keys)


def drive_meta_all():
    """Enter: the primary button, "Remove all metadata"."""
    def keys(win):
        tool_shot("PDF Metadata")
        inject.press(C.shell, "Return")
    drive("Metadata", keys)


# ------------------------------------------------------------------ stricter checks
def _and(base, extra):
    """The shared verifier, then a Linux check on the same output; both notes."""
    def v(outs, srcs):
        a = base(outs, srcs)
        b = extra(outs, srcs)
        return f"{a}; {b}" if b else a
    return v


def _texts(path: Path) -> list[str]:
    import pymupdf
    with pymupdf.open(path) as d:
        return [p.get_text().strip() for p in d]


def x_compress(outs, srcs):
    s0, s1 = Path(srcs[0]).stat().st_size, outs[0].stat().st_size
    assert len(outs) == 1 and outs[0].name == "Report Compressed.pdf", [o.name for o in outs]
    assert s1 < s0, f"not smaller: {s0} → {s1} bytes"
    assert _texts(outs[0]) == _texts(Path(srcs[0])), "the text changed"
    return f"{(1 - s1 / s0) * 100:.0f}% smaller, same text"


def x_split(outs, srcs):
    src = _texts(Path(srcs[0]))
    pdfs = sorted(outs, key=lambda o: o.name)
    want = [f"Report page {i}.pdf" for i in range(1, len(src) + 1)]
    assert [o.name for o in pdfs] == want, [o.name for o in pdfs]
    for o, text in zip(pdfs, src):
        assert pdf_pages(o) == 1, f"{o.name}: {pdf_pages(o)} pages"
        assert _texts(o) == [text], f"{o.name} is not that page"
    return "1 page each, in order"


def x_rotated(outs, srcs):
    assert pdf_pages(outs[0]) == pdf_pages(Path(srcs[0]))
    assert _texts(outs[0]) == _texts(Path(srcs[0])), "the text changed"
    return f"{pdf_pages(outs[0])} pages, same text"


def x_protected(outs, srcs):
    import pymupdf
    with pymupdf.open(outs[0]) as d:
        assert d.needs_pass, "readable without a password"
        assert not d.authenticate("wrong password"), "a wrong password opens it"
        assert d.authenticate(PASSWORD)
        enc = d.metadata.get("encryption") or ""
        assert "AES" in enc and "256" in enc, f"encryption {enc!r}"
        assert d.page_count == pdf_pages(Path(srcs[0]))
        assert [p.get_text().strip() for p in d] == _texts(Path(srcs[0])), "the text changed"
    return f"{enc}, wrong password refused, {pdf_pages(Path(srcs[0]))} pages"


def x_extract(outs, srcs):
    """As many images as the source embeds (distinct, ≥ 16 px wide), each at its size."""
    import pymupdf
    from PIL import Image
    with pymupdf.open(srcs[0]) as d:
        xrefs = {i[0]: (i[2], i[3]) for p in d for i in p.get_images(full=True) if i[2] >= 16}
    assert len(outs) == len(xrefs), f"{len(outs)} images, the PDF embeds {len(xrefs)}"
    sizes = []
    for o in outs:
        with Image.open(o) as im:
            sizes.append(im.size)
    assert sorted(sizes) == sorted(xrefs.values()), f"{sizes} != {list(xrefs.values())}"
    return "sizes match the embedded images " + ", ".join(f"{w}×{h}" for w, h in sizes)


def x_pdf_meta(outs, srcs):
    import pymupdf
    with pymupdf.open(srcs[0]) as d:
        before = {k: v for k, v in d.metadata.items() if v and k not in ("format", "encryption")}
    with pymupdf.open(outs[0]) as d:
        left = {k: v for k, v in d.metadata.items() if v and k not in ("format", "encryption")}
        xmp = d.get_xml_metadata()
        pages = d.page_count
    assert before, "the sample PDF has no metadata to remove"
    assert not left, f"left: {left}"
    assert not xmp, "XMP metadata left"
    assert pages == pdf_pages(Path(srcs[0]))
    return f"removed {', '.join(sorted(before))}; no XMP"


def x_merged(outs, srcs):
    """Both PDFs whole, one after the other (in either order: the drop's file order)."""
    got = _texts(outs[0])
    a, b = (_texts(Path(s)) for s in srcs)
    assert got in (a + b, b + a), "pages are not the two PDFs one after the other"
    first = Path(srcs[0] if got[:len(a)] == a else srcs[1]).name
    return f"{first} first"


# ------------------------------------------------------------------ scenarios
SCENARIOS: list[Scenario] = [
    Scenario("PDF Compress", "nautilus", ["Report.pdf"], "Tools", "Compress",
             _and(v_pdf_compress, x_compress)),
    Scenario("PDF Split", "nautilus", ["Report.pdf"], "Tools", "Split", _and(v_pdf_n(3), x_split)),
    Scenario("PDF Rotate", "nautilus", ["Report.pdf"], "Tools", "Rotate", _and(v_rotated, x_rotated)),
    Scenario("PDF Protect", "nautilus", ["Report.pdf"], "Tools", "Protect",
             _and(v_protected, x_protected), drive_protect),
    Scenario("PDF Extract images", "nautilus", ["Report.pdf"], "Tools", "Extract",
             _and(v_extract, x_extract)),
    Scenario("PDF Metadata", "nautilus", ["Report.pdf"], "Tools", "Metadata",
             _and(v_pdf_meta, x_pdf_meta), drive_meta_all),
    Scenario("Merge 2 PDFs", "nautilus", ["Appendix.pdf", "Report.pdf"], "Tools", "Merge",
             _and(v_merged, x_merged), select_extra=True),
]
