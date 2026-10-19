"""Drops from Files and the desktop onto the radial menu (#35), in the isolated GNOME
session: the real app (`python -m dragshift`) with DragShift's extension, and real
Shift+drags injected through the test-helper extension.

A drag opens the menu (#31), the file enters it, the pointer moves to a petal computed
from `RadialMenu`'s own geometry, and the button goes up there. The action then runs
(`activated` → `DragShiftApp.run_item`) and its result appears next to the original:

- one file, from Files and from the desktop: JPG → PNG and PNG → JPG, also with spaces,
  non-ASCII letters and `#` in the name (the URI is percent-encoded);
- two files (multi-select): the hub shows `2 files`, and `PDF` makes one 2-page PDF;
- the Tools ring, flipped by a Shift tap and by hovering the hub (PDF → Rotate, Compress);
- a drop beside the petals or on the hub, and a file type with no petals: nothing runs;
- a mixed selection: the petal applies to the image only, the text file is left alone.

In every case the dragged files keep their bytes and mtime, and no file other than the
expected output appears. Outputs are checked like the Windows E2E verifiers do
(`img_ok`: Pillow loads it, format and size; `pdf_pages`). With DRAGSHIFT_EVIDENCE_DIR
set, screenshots go there."""
import io
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift.radial import R_IN, R_OUT, SIZE, angles
from tests.linux import inject
from tests.linux.conftest import require_session_tools
from tests.linux.files import Files
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.test_extension import (ROOT, _has_owner, _state, _wait,  # noqa: F401
                                        built_dir, built_zip)
from tests.linux.test_trigger import _menus, _press_and_move, _settled_frame, _wait_menu

pytestmark = pytest.mark.gnome

FOLLOWING = "trigger: following the extension's pointer state"
R_PETAL = (R_IN + R_OUT) / 2   # 109, as in demo/e2e_windows.py
CORNER = 40                    # px into the menu window: inside it, outside every petal
NO_OUTPUT_WAIT = 3.0           # s to wait for an output that must not appear
TOAST = "DragShift toast"
NAUTILUS_TOWARD = (1, 0.3)     # where the drag goes from the icon: free space
DESKTOP_TOWARD = (-1, -0.6)


def _image(fmt: str, size=(64, 48), color=(220, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    return buf.getvalue()


def _pdf(pages: int = 2) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=300, height=200).insert_text((40, 100), f"Page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


JPG = _image("JPEG")
PNG = _image("PNG", (80, 60), (30, 160, 90))
JPG_B = _image("JPEG", (48, 64), (40, 40, 200))
PDF = _pdf()


def _evidence(path: Path, name: str) -> None:
    if os.environ.get("DRAGSHIFT_EVIDENCE_DIR"):
        shutil.copy(path, Path(os.environ["DRAGSHIFT_EVIDENCE_DIR"]) / name)


# ------------------------------------------------------------------ verifiers (E2E logic)
def img_ok(p: Path, fmt=None):
    with Image.open(p) as im:
        im.load()
        if fmt:
            assert im.format == fmt, f"{p.name}: format {im.format} != {fmt}"
        return im.size


def pdf_pages(p: Path) -> int:
    import pymupdf
    with pymupdf.open(p) as d:
        return d.page_count


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def session(built_dir):  # noqa: F811
    require_session_tools()
    with Session(extensions=[built_dir], helper=True) as s:
        yield s


@pytest.fixture(scope="module")
def shell(session):
    with ShellHelper(session) as helper:
        helper.wait_ready()
        assert _wait(lambda: _has_owner(session)), _state(session)
        yield helper


@pytest.fixture(scope="module")
def app(session, shell):
    """DragShift started the way a user starts it (no Welcome window)."""
    s = session
    conf = Path(s.env()["XDG_CONFIG_HOME"]) / "DragShift" / "DragShift.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[General]\nwelcomed=true\n")
    out = s.root / "tmp" / "dragshift-drop.out"
    text = lambda: out.read_text(errors="replace")         # noqa: E731
    with open(out, "w") as f:
        proc = s.popen([sys.executable, "-m", "dragshift", "--quiet"], cwd=ROOT,
                       env={"PYTHONPATH": str(ROOT)}, stdout=f, stderr=subprocess.STDOUT)
    try:
        assert _wait(lambda: FOLLOWING in text(), 20), text()
        yield {"proc": proc, "text": text}
    finally:
        shell.release_all()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _evidence(out, "app-log.txt")


@pytest.fixture
def files(session, shell, app):
    f = Files(session, shell)
    yield f
    shell.release_all()
    f.close()
    _no_toasts(shell, app)


class Source:
    """Files in a new Files window or on the desktop, with what they hold now, so the test
    can prove afterwards that nothing moved or changed them."""

    def __init__(self, files: Files, where: str, contents: dict[str, bytes]):
        self.files, self.where = files, where
        if where == "nautilus":
            self.folder = files.make_folder(f"drop-{secrets.token_hex(3)}", contents)
            files.open_folder(self.folder)
            self.toward = NAUTILUS_TOWARD
        else:
            self.folder = files.put_on_desktop(contents)[0].parent
            self.toward = DESKTOP_TOWARD
        self.paths = [self.folder / n for n in contents]
        self.contents = dict(contents)
        self.stat = {p: p.stat().st_mtime_ns for p in self.paths}
        self.before = {p.name for p in self.folder.iterdir()}

    def icon(self, name: str) -> tuple[int, int]:
        return self.files.icon_center(name, self.where)

    def new_files(self, timeout: float = 15.0, wait_for_any: bool = True) -> list[Path]:
        """Files that appeared since the start, once their sizes stop changing (the E2E
        runners' `new_files`)."""
        def current():
            return {p: p.stat().st_size for p in self.folder.iterdir()
                    if p.name not in self.before and p.is_file()
                    and not p.name.startswith(".~")}
        end = time.monotonic() + timeout
        last = current()
        while time.monotonic() < end:
            time.sleep(0.5)
            now = current()
            if now and now == last:
                return sorted(now)
            if not now and not wait_for_any:
                return []
            last = now
        return sorted(current())

    def check_untouched(self) -> None:
        for p in self.paths:
            assert p.exists(), f"{p.name} was moved or deleted"
            assert p.read_bytes() == self.contents[p.name], f"{p.name} changed"
            assert p.stat().st_mtime_ns == self.stat[p], f"{p.name} was rewritten"

    def cleanup(self, outs=()) -> None:
        if self.where == "desktop":
            # one drag per desktop icon: DING keeps a dragged icon selected
            for p in [*self.paths, *outs]:
                p.unlink(missing_ok=True)


# ------------------------------------------------------------------ helpers
def _no_toasts(shell, app, timeout: float = 15.0) -> None:
    """Wait until the result cards are gone: they sit bottom right, over the desktop icons."""
    _wait(lambda: not [w for w in shell.list_windows() if w["pid"] == app["proc"].pid
                       and w["title"].startswith(TOAST)], timeout)


def _rings(paths: list[Path]) -> dict:
    from dragshift.app import DragShiftApp
    return DragShiftApp.build_rings(None, [str(p) for p in paths])


def _petal(frame, n: int, i: int) -> tuple[int, int]:
    """Stage point at the middle of petal i of n (the menu's own geometry)."""
    a0, a1 = angles(n, i)
    m = math.radians((a0 + a1) / 2)
    x, y = frame[0] + SIZE / 2, frame[1] + SIZE / 2
    return round(x + R_PETAL * math.cos(m)), round(y - R_PETAL * math.sin(m))


def _centre(frame) -> tuple[int, int]:
    return frame[0] + SIZE // 2, frame[1] + SIZE // 2


def _select(shell, src: Source, names: list[str]) -> None:
    """Click the first file, Ctrl+click the others (multi-select), like a user."""
    inject.click(shell, *src.icon(names[0]))
    for name in names[1:]:
        time.sleep(0.3)
        with inject.hold_key(shell, "Control_L"):
            inject.click(shell, *src.icon(name))
    time.sleep(0.8)    # longer than the double-click time: the drag's press opens nothing


def _shot(session, shell, tag: str | None, step: str) -> None:
    if tag:
        name = f"{tag}-{step}.png"
        _evidence(shell.screenshot(session.root / "tmp" / name), name)


def _shift_drag(session, shell, app, src: Source, drag: str, *, select=(), flip=None,
                to=None, tag=None, peek=False) -> tuple[str, tuple]:
    """A real Shift+drag of `drag` (after selecting `select`) into the menu; then an
    optional ring flip ("shift": a Shift tap, "hub": visit a petal, then rest on the hub)
    and the drop at `to`: (ring, label) for a petal, "corner", "hub", or (n, i) for where
    petal i of n would be. peek: first move the file to a corner of the menu, where the
    drag icon leaves the hub's text visible, for a screenshot. Returns the app's log of
    the gesture and the menu's frame."""
    mark = len(app["text"]())
    log = lambda: app["text"]()[mark:]         # noqa: E731
    try:
        if select:
            _select(shell, src, list(select))
        _press_and_move(shell, src.icon(drag), src.toward, shift_first=False)
        menu, _ = _wait_menu(shell, app, 3.0)
        assert menu is not None, log()
        frame = _settled_frame(shell, app)
        inject.glide(shell, *_centre(frame), dur=0.15)        # the file enters the menu
        assert _wait(lambda: re.search(r"menu: \d+ file\(s\)", log()), 3), log()
        time.sleep(0.2)
        assert _menus(shell, app), "the menu closed before the drop"
        _shot(session, shell, tag, "entered")
        if peek:
            inject.glide(shell, frame[0] + CORNER, frame[1] + CORNER, dur=0.3)
            time.sleep(0.3)
            _shot(session, shell, tag, "hub")
        rings = _rings(src.paths if not select else [src.folder / n for n in select])
        names = [k for k, v in rings.items() if v]
        if flip == "shift":
            shell.key(inject.KEYSYMS["Shift_L"], False)
            time.sleep(0.1)
            shell.key(inject.KEYSYMS["Shift_L"], True)
            assert _wait(lambda: f"ring: {names[1]}" in log(), 2), log()
        elif flip == "hub":
            inject.glide(shell, *_petal(frame, len(rings[names[0]]), 0), dur=0.25)
            time.sleep(0.15)
            inject.glide(shell, *_centre(frame), dur=0.25)
            assert _wait(lambda: f"menu: hub hover, ring: {names[1]}" in log(), 2), log()
        time.sleep(0.25)                                      # the ring-swap animation
        if to == "corner":
            target = (frame[0] + CORNER, frame[1] + CORNER)
        elif to == "hub":
            target = _centre(frame)
        elif isinstance(to[0], int):
            target = _petal(frame, *to)
        else:
            ring, label = to
            labels = [it.label for it in rings[ring]]
            target = _petal(frame, len(labels), labels.index(label))
        inject.glide(shell, *target, dur=0.3)
        time.sleep(0.35)
        _shot(session, shell, tag, "over")
        shell.button(1, False)                                # the drop
        time.sleep(0.05)
        shell.key(inject.KEYSYMS["Shift_L"], False)
        assert _wait(lambda: not _menus(shell, app), 2), "the menu stayed open"
    finally:
        shell.release_all()
    return log(), frame


def _assert_dropped(log: str, n: int, label: str, ring: str) -> None:
    """The drop landed on that petal, and the menu answered Copy (a Shift+drag is a move
    for Files and the desktop: Move would take the file out of its folder)."""
    m = re.search(rf"menu: dropped {n} file\(s\) on {re.escape(label)} \({ring}\), "
                  r"proposed (\w+), answered (\w+)", log)
    assert m, log
    print(f"\n{label}: proposed {m[1]}, answered {m[2]}")
    assert m[2] == "CopyAction", log


def _expect_one(src: Source, name: str) -> Path:
    outs = src.new_files()
    assert [p.name for p in outs] == [name], [p.name for p in outs]
    return outs[0]


# ------------------------------------------------------------------ tests
ONE_FILE = [
    # where, file name, contents, petal, output, PIL format, size
    ("nautilus", "Photo.jpg", JPG, "PNG", "Photo.png", "PNG", (64, 48)),
    ("desktop", "Shot {t}.png", PNG, "JPG", "Shot {t}.jpg", "JPEG", (80, 60)),
    ("nautilus", "Été café #2 ünï.png", PNG, "JPG", "Été café #2 ünï.jpg", "JPEG", (80, 60)),
    ("desktop", "My photo #{t} – ½.jpg", JPG, "PNG", "My photo #{t} – ½.png", "PNG", (64, 48)),
]


@pytest.mark.parametrize("where,name,data,label,out,fmt,size", ONE_FILE,
                         ids=["nautilus-jpg-png", "desktop-png-jpg", "nautilus-odd-name",
                              "desktop-odd-name"])
def test_one_file_onto_a_petal(session, shell, app, files, where, name, data, label, out,
                               fmt, size):
    t = secrets.token_hex(2)
    name, out = name.format(t=t), out.format(t=t)
    src = Source(files, where, {name: data})
    outs = []
    try:
        tag = f"one-{where}-{'odd' if '#' in name else label.lower()}"
        log, _ = _shift_drag(session, shell, app, src, name, to=("Convert", label), tag=tag)
        assert "menu: 1 file(s)" in log
        _assert_dropped(log, 1, label, "Convert")
        outs = [_expect_one(src, out)]
        assert img_ok(outs[0], fmt) == size
        _shot(session, shell, tag, "result")
        src.check_untouched()
        print(f"{where}: {name!r} → {out!r}, {fmt} {size[0]}x{size[1]}, source unchanged")
    finally:
        src.cleanup(outs)


@pytest.mark.parametrize("where", ["nautilus", "desktop"])
def test_two_files_onto_pdf_make_one_pdf(session, shell, app, files, where):
    t = secrets.token_hex(2)
    names = [f"Photo A{t}.jpg", f"Photo B{t}.jpg"]
    src = Source(files, where, {names[0]: JPG, names[1]: JPG_B})
    outs = []
    try:
        pdf = [it for it in _rings(src.paths)["Convert"] if it.label == "PDF"][0]
        assert pdf.hint == "2 images → 1 PDF"
        log, _ = _shift_drag(session, shell, app, src, names[1], select=names,
                             to=("Convert", "PDF"), tag=f"two-{where}", peek=True)
        assert "menu: 2 file(s), hub '2 files'" in log, log
        _assert_dropped(log, 2, "PDF", "Convert")
        outs = src.new_files()
        # the first file of the drop names it; Files and the desktop list them in view order
        assert [p.name for p in outs] in ([f"Photo A{t} Combined.pdf"],
                                          [f"Photo B{t} Combined.pdf"]), outs
        assert pdf_pages(outs[0]) == 2
        src.check_untouched()
        print(f"{where}: 2 files → {outs[0].name!r}, 2 pages, sources unchanged")
    finally:
        src.cleanup(outs)


@pytest.mark.parametrize("where,flip,label,out", [
    ("nautilus", "shift", "Rotate", "Doc{t} Rotated.pdf"),
    ("desktop", "hub", "Compress", "Doc{t} Compressed.pdf"),
], ids=["nautilus-shift-tap", "desktop-hub-hover"])
def test_tools_ring(session, shell, app, files, where, flip, label, out):
    import pymupdf
    t = secrets.token_hex(2)
    name, out = f"Doc{t}.pdf", out.format(t=t)
    src = Source(files, where, {name: PDF})
    outs = []
    try:
        log, _ = _shift_drag(session, shell, app, src, name, flip=flip, to=("Tools", label),
                             tag=f"tools-{flip}")
        _assert_dropped(log, 1, label, "Tools")
        outs = [_expect_one(src, out)]
        assert pdf_pages(outs[0]) == 2
        if label == "Rotate":
            with pymupdf.open(outs[0]) as d:
                assert all(p.rotation == 90 for p in d)
        src.check_untouched()
        print(f"{where}: ring flipped by {flip}, {label} → {out!r}, 2 pages, source unchanged")
    finally:
        src.cleanup(outs)


@pytest.mark.parametrize("to", ["corner", "hub"])
def test_drop_beside_the_petals_runs_nothing(session, shell, app, files, to):
    src = Source(files, "nautilus", {"Photo.jpg": JPG})
    log, _ = _shift_drag(session, shell, app, src, "Photo.jpg", to=to, tag=f"beside-{to}")
    assert "menu: dropped beside the petals, nothing to run" in log, log
    assert "menu: dropped 1 file" not in log
    assert src.new_files(NO_OUTPUT_WAIT, wait_for_any=False) == []
    src.check_untouched()


def test_type_without_petals_runs_nothing(session, shell, app, files):
    src = Source(files, "nautilus", {"Archive.zip": b"PK\x05\x06" + b"\0" * 18})
    assert not any(_rings(src.paths).values())
    log, _ = _shift_drag(session, shell, app, src, "Archive.zip", to=(8, 0),
                         tag="no-petals")
    assert "rings: none" in log, log
    assert "menu: dropped beside the petals, nothing to run" in log, log
    assert src.new_files(NO_OUTPUT_WAIT, wait_for_any=False) == []
    src.check_untouched()


def test_mixed_selection_runs_on_the_files_the_petal_applies_to(session, shell, app, files):
    """An image and a text file: the menu is the image's, and PNG converts the image only;
    the text file (dragged, last selected) is left as it is."""
    names = ["A photo.jpg", "B notes.txt"]
    src = Source(files, "nautilus", {names[0]: JPG, names[1]: b"just some notes\n"})
    log, _ = _shift_drag(session, shell, app, src, names[1], select=names,
                         to=("Convert", "PNG"), tag="mixed")
    assert "menu: 2 file(s)" in log, log
    _assert_dropped(log, 2, "PNG", "Convert")
    out = _expect_one(src, "A photo.png")
    assert img_ok(out, "PNG") == (64, 48)
    src.check_untouched()
