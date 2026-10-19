"""Shows one of the app's toast cards in a process of its own, inside the isolated session
(tests/linux/test_toasts.py, test_toast_actions.py).

It is a module rather than a `python -c` string because the shell extension answers
GetWorkArea and PlaceWindow only to the DragShift app, and, in a test session, to the
test processes it recognises by their command line (`python -m tests.linux.<module>`,
#93); `python -c` is refused.

    python -m tests.linux.toast_driver done <out file>
        a "Converted" card for <out file>; prints {"t": time.time()} once shown, then
        lives 8 s
    python -m tests.linux.toast_driver click <button> <work dir>
        converts a picture in <work dir>, clicks <button> on the result card and prints
        {"outs", "buttons", "state", "title", "detail"}
"""
import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def done(out: str) -> None:
    from dragshift import report, toasts
    out = Path(out)
    out.write_bytes(b"x")
    card = toasts.ToastStack().new()
    card.done(report.Result(title="Converted", detail="photo.png → photo.jpg", outs=[out]))
    print(json.dumps({"t": time.time()}), flush=True)
    spin(8000)


def click(button: str, work: str) -> None:
    from PIL import Image
    from PySide6.QtTest import QTest
    from dragshift import convert, report, toasts
    work = Path(work)
    work.mkdir(parents=True)
    src = work / "photo.png"
    Image.new("RGB", (64, 48), (200, 80, 40)).save(src)
    outs = convert.image_to(src, "JPG")
    card = toasts.ToastStack().new()
    card.done(report.summarize("JPG", [src], outs))
    spin(500)                                   # painted: card.buttons now holds the rects
    rect = dict(card.buttons)[button]
    QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, rect.center().toPoint())
    spin(1500)                                  # let the D-Bus reply / xdg-open happen
    print(json.dumps({"outs": [str(o) for o in outs],
                      "buttons": [n for n, _ in card.buttons],
                      "state": card.state, "title": card.title, "detail": card.detail}))


if __name__ == "__main__":
    app = QApplication([])                      # before any dragshift widget module
    {"done": done, "click": click}[sys.argv[1]](*sys.argv[2:])
