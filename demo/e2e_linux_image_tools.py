"""Image tool scenarios for demo/e2e_linux.py (#61): the twins of e2e_windows.py's
"image tools" block. Each drags Photo.jpg from Files onto a Tools petal, drives the glass
tool window that opens with injected input (controls found by label over AT-SPI, see
e2e_linux.tool_window) and checks the output with the same verifier as on Windows.
"Compress: quality 40" has no Windows twin: it covers the window's other mode."""
from __future__ import annotations

import io
import time
from pathlib import Path

from PIL import Image, ImageChops

from e2e_common import (img_ok, v_compress, v_dims, v_exists, v_no_gps_keep_camera, v_no_meta,
                        v_redact)
from e2e_linux import C, Scenario, inject, tool_window


def drive_compress():
    w = tool_window("Compress")
    w.click("Target size", checked=True)
    w.click("200 KB", checked=True)
    w.click("Compress", primary=True)
    w.wait_closed()


QUALITY = 40   # Home (10), then three Page_Up (the slider's page step is 10)


def drive_compress_quality():
    """The Quality mode (the default): the slider set with the keyboard to QUALITY."""
    w = tool_window("Compress")
    x, y, bw, bh = w.find("", "slider")["box"]
    inject.click(C.shell, x + bw // 2, y + bh // 2, dur=0.25)    # focus the slider
    inject.press(C.shell, "Home")
    for _ in range(3):
        inject.press(C.shell, "Page_Up")

    def shown():           # the value label right after the slider
        items = w.items()
        i = next(k for k, it in enumerate(items) if it["role"] == "slider")
        return items[i + 1]["name"]
    end = time.monotonic() + 2
    while shown() != str(QUALITY):
        if time.monotonic() > end:
            raise AssertionError(f"the quality slider shows {shown()}, not {QUALITY}")
        time.sleep(0.1)
    w.click("Compress", primary=True)
    w.wait_closed()


def v_quality(q):
    """A JPEG of the source's size, byte for byte as large as the source re-encoded at
    quality q by the app's own encoder: the slider's value is what was used."""
    def v(outs, srcs):
        from dragshift.convert import open_image, save_image
        o = outs[0]
        assert o.suffix.lower() == ".jpg", f"extension {o.suffix}"
        assert img_ok(o, "JPEG") == img_ok(Path(srcs[0])), "size differs from the source"
        buf = io.BytesIO()
        save_image(open_image(srcs[0]), buf, "JPG", quality=q)
        s0, s1, ref = Path(srcs[0]).stat().st_size, o.stat().st_size, buf.tell()
        assert s1 == ref, f"{s1} bytes, the source at quality {q} is {ref}"
        assert s1 < s0, f"{s1} bytes is not smaller than {s0}"
        return f"quality {q}: {s0 // 1024} KB → {s1 // 1024} KB"
    return v


def drive_resize():
    w = tool_window("Resize")
    w.click("50%", checked=True)
    w.click("Resize", primary=True)
    w.wait_closed()


def drive_crop():
    w = tool_window("Crop Image")
    w.click("1:1", checked=True)
    w.click("Apply", primary=True)
    w.wait_closed()


def drive_edit():
    w = tool_window("Edit")
    w.click("✦ Auto")
    w.click("⟳")
    w.click("Save", primary=True)
    w.wait_closed()


def _middle(w):
    x0, y0, x1, y1 = w.canvas()
    return (x0 + x1) // 2, (y0 + y1) // 2


def drive_annotate():
    w = tool_window("Annotate")
    cx, cy = _middle(w)
    w.drag((cx - 150, cy + 80), (cx + 120, cy - 60))      # the default tool: an arrow
    w.click("① Step", checked=True)
    inject.click(C.shell, cx + 160, cy - 80, dur=0.25)      # a click drops step 1
    w.click("Save", primary=True)
    w.wait_closed()


def drive_redact():
    w = tool_window("Redact")
    cx, cy = _middle(w)
    w.drag((cx - 120, cy - 60), (cx + 120, cy + 60))      # a black box over the centre
    w.click("Save", primary=True)
    w.wait_closed()


def drive_addbg():
    w = tool_window("Add BG")
    w.click("1:1", checked=True)
    w.click("Save with Background", primary=True)
    w.wait_closed()


def drive_meta_location():
    w = tool_window("Metadata")
    w.shot()
    w.click("Remove location only")
    w.wait_closed()


def drive_meta_all():
    w = tool_window("Metadata")
    w.click("Remove all metadata", primary=True)
    w.wait_closed()


def v_annotated(outs, srcs):
    """v_exists (the Windows check), and the drawing is in the pixels: same size as the
    source, with thousands of pixels clearly changed (the arrow and the step badge), not
    just JPEG noise."""
    note = v_exists(outs, srcs)
    with Image.open(srcs[0]) as a, Image.open(outs[0]) as b:
        assert a.size == b.size, f"{a.size} → {b.size}"
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    changed = sum(diff.point(lambda v: 255 if v > 60 else 0).histogram()[255:])
    assert changed > 2000, f"only {changed} pixels changed"
    return f"{note} · {changed} pixels drawn on"


def _tool(name, label, verify, driver):
    return Scenario(name, "nautilus", ["Photo.jpg"], "Tools", label, verify, driver)


SCENARIOS = [
    _tool("Compress to 200 KB", "Compress", v_compress(200_000), drive_compress),
    _tool(f"Compress: quality {QUALITY}", "Compress", v_quality(QUALITY), drive_compress_quality),
    _tool("Resize 50%", "Resize", v_dims(lambda a, b: b == (a[0] // 2, a[1] // 2)), drive_resize),
    _tool("Crop 1:1", "Crop", v_dims(lambda a, b: b[0] == b[1] == min(a)), drive_crop),
    _tool("Edit: auto + rotate", "Edit", v_dims(lambda a, b: b == (a[1], a[0])), drive_edit),
    _tool("Annotate", "Annotate", v_annotated, drive_annotate),
    _tool("Redact", "Redact", v_redact, drive_redact),
    _tool("Add BG 1:1", "Add BG", v_dims(lambda a, b: b[0] == b[1]), drive_addbg),
    _tool("Metadata: remove location", "Metadata", v_no_gps_keep_camera, drive_meta_location),
    _tool("Metadata: remove all", "Metadata", v_no_meta, drive_meta_all),
]
