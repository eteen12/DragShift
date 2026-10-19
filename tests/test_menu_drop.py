"""The radial menu's drop path, offscreen, with synthetic drag events (#35).

A file manager hands the dropped files over as a percent-encoded `text/uri-list`; the menu
reads them when the drag enters, answers Copy (a Shift+drag proposes Move, and a move
would take the file away from its folder), and runs the petal under the drop. The real
drags from Files and the desktop are in tests/linux/test_menu_drop.py."""
import sys
import warnings
from pathlib import Path

import pytest

LINUX = sys.platform.startswith("linux")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def menu(qapp):
    from dragshift import radial
    m = radial.RadialMenu(
        lambda p: {"Convert": [radial.Item(t, key=f"convert:{t}") for t in ("PNG", "WEBP", "PDF", "GIF")],
                   "Tools": [radial.Item("Compress", key="tool:Compress")]},
        lambda p: f"{len(p)} files" if len(p) > 1 else "1 KB")
    m.fired, m.closed = [], []
    # as Path objects: on Windows toLocalFile() gives C:/..., which Path compares equal
    m.activated.connect(lambda item, paths: m.fired.append((item.label, [Path(p) for p in paths])))
    m.dismissed.connect(lambda: m.closed.append(True))
    m.resize(radial.SIZE, radial.SIZE)
    yield m
    m.hide()


def _uri(path) -> str:
    """The file:// URI, percent-encoded the way Files and the desktop send it."""
    from PySide6.QtCore import QUrl
    return bytes(QUrl.fromLocalFile(str(path)).toEncoded()).decode()


def _mime(uris: list[str]):
    from PySide6.QtCore import QMimeData
    md = QMimeData()
    md.setData("text/uri-list", "".join(u + "\r\n" for u in uris).encode())
    return md


def _event(kind: str, pos, md, *, shift=False, actions=None):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    actions = actions if actions is not None else Qt.CopyAction | Qt.MoveAction
    mods = Qt.ShiftModifier if shift else Qt.NoModifier
    with warnings.catch_warnings():          # PySide6 marks the QPoint constructors deprecated
        warnings.simplefilter("ignore", DeprecationWarning)
        if kind == "enter":
            return QDragEnterEvent(QPoint(*pos), actions, md, Qt.LeftButton, mods)
        if kind == "move":
            return QDragMoveEvent(QPoint(*pos), actions, md, Qt.LeftButton, mods)
        return QDropEvent(QPointF(*pos), actions, md, Qt.LeftButton, mods)


def _petal(n: int, i: int) -> tuple[int, int]:
    import math
    from dragshift import radial
    a0, a1 = radial.angles(n, i)
    m = math.radians((a0 + a1) / 2)
    r, c = (radial.R_IN + radial.R_OUT) / 2, radial.SIZE / 2
    return round(c + r * math.cos(m)), round(c - r * math.sin(m))


def _flush(qapp):
    import time
    for _ in range(5):
        qapp.processEvents()
        time.sleep(0.01)


def test_paths_are_percent_decoded_and_non_local_uris_ignored(menu, tmp_path):
    names = ["My photo #1 – é.jpg", "100% ünï & co.png", "plain.jpg"]
    files = [tmp_path / n for n in names]
    uris = [_uri(f) for f in files]
    assert "%20" in uris[0] and "%23" in uris[0] and "%25" in uris[1]   # as Files sends them
    md = _mime([uris[0], "sftp://host/remote.jpg", uris[1], "https://example.org/a.png", uris[2]])
    e = _event("enter", (200, 200), md)
    menu.dragEnterEvent(e)
    assert e.isAccepted()
    assert [Path(p) for p in menu.paths] == files
    assert menu.hub_text == "3 files"


def test_only_remote_files_are_refused(menu):
    md = _mime(["sftp://host/a.jpg", "smb://nas/b.png"])   # events only point at it: keep it
    e = _event("enter", (200, 200), md)
    menu.dragEnterEvent(e)
    assert not e.isAccepted()
    assert not menu.got_drag and menu.paths == []


def test_a_shift_drag_is_answered_with_copy(menu, qapp, tmp_path):
    """Shift proposes Move; every step answers Copy, so the source never loses the file."""
    from PySide6.QtCore import Qt
    f = tmp_path / "a.jpg"
    md = _mime([_uri(f)])
    enter = _event("enter", (200, 200), md, shift=True)
    assert enter.proposedAction() == Qt.MoveAction
    menu.dragEnterEvent(enter)
    move = _event("move", _petal(4, 0), md, shift=True)
    menu.dragMoveEvent(move)
    drop = _event("drop", _petal(4, 0), md, shift=True)
    menu.dropEvent(drop)
    for e in (enter, move, drop):
        assert e.isAccepted() and e.dropAction() == Qt.CopyAction
    _flush(qapp)
    assert menu.fired == [("PNG", [f])]


def test_the_drop_runs_the_paths_read_at_enter(menu, qapp, tmp_path):
    """The desktop can send an empty payload with the drop (#19): the paths from the enter
    event are the ones that run."""
    from PySide6.QtCore import QMimeData
    files = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    md = _mime([_uri(f) for f in files])
    menu.dragEnterEvent(_event("enter", (200, 200), md))
    empty = QMimeData()
    drop = _event("drop", _petal(4, 2), empty)
    menu.dropEvent(drop)
    _flush(qapp)
    assert menu.fired == [("PDF", files)]


@pytest.mark.parametrize("where", ["corner", "hub", "outside the petals"])
def test_a_drop_beside_the_petals_runs_nothing(menu, qapp, tmp_path, where):
    md = _mime([_uri(tmp_path / "a.jpg")])
    menu.show()
    menu.dragEnterEvent(_event("enter", (200, 200), md))
    pos = {"corner": (12, 12), "hub": (200, 200), "outside the petals": (200, 6)}[where]
    drop = _event("drop", pos, md)
    menu.dropEvent(drop)
    _flush(qapp)
    assert not drop.isAccepted()
    assert menu.fired == [] and menu.closed == [True] and not menu.isVisible()


def test_a_file_type_without_petals_runs_nothing(qapp, tmp_path):
    from dragshift import radial
    m = radial.RadialMenu(lambda p: {"Unsupported": []}, lambda p: "22 B")
    fired = []
    m.activated.connect(lambda *a: fired.append(a))
    md = _mime([_uri(tmp_path / "a.zip")])
    m.dragEnterEvent(_event("enter", (200, 200), md))
    assert m.items == [] and m.hub_text == "22 B"
    m.dropEvent(_event("drop", _petal(8, 0), md))
    _flush(qapp)
    assert fired == []


@pytest.mark.skipif(not LINUX, reason="Linux: menus clamped to GNOME's work area")
def test_entering_on_a_petal_does_not_arm_the_hub_flip(menu, tmp_path):
    """On GNOME the menu is often clamped (desktop icons are at the right edge), so the
    file enters on a petal. That is no visit: resting on the hub afterwards keeps the ring.
    A petal the pointer then moves onto arms it, as before."""
    md = _mime([_uri(tmp_path / "a.jpg")])
    menu.dragEnterEvent(_event("enter", _petal(4, 1), md))
    menu.dragMoveEvent(_event("move", _petal(4, 1), md))
    assert menu.hover == 1 and not menu.hub_armed
    menu.dragMoveEvent(_event("move", (200, 200), md))
    assert menu.hover == -2 and not menu.hub_timer.isActive()
    menu.dragMoveEvent(_event("move", _petal(4, 2), md))
    assert menu.hub_armed
    menu.dragMoveEvent(_event("move", (200, 200), md))
    assert menu.hub_timer.isActive()
