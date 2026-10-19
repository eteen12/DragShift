"""Throwaway #95 probe: show a window with a given Qt window type under native Wayland."""
import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QWidget

variant = sys.argv[1]
app = QApplication([sys.argv[0], "-platform", "wayland"])
app.setDesktopFileName("io.github.eteen12.DragShift")
MENU = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
keep = []


def mk(flags, parent=None, title=None, cls=QWidget):
    w = cls(parent) if parent is not None else cls()
    w.setWindowFlags(flags)
    w.setWindowTitle(title or f"probe {variant}")
    w.setAttribute(Qt.WA_ShowWithoutActivating)
    w.resize(200, 200)
    w.setStyleSheet("background: #c04080;")
    keep.append(w)
    return w


if variant == "menu-flags":
    mk(MENU).show()
elif variant == "tooltip":
    mk(Qt.ToolTip | Qt.FramelessWindowHint).show()
elif variant == "popup":
    mk(Qt.Popup | Qt.FramelessWindowHint).show()
elif variant == "splash":
    mk(Qt.SplashScreen | Qt.FramelessWindowHint).show()
elif variant == "tool-with-parent":
    p = mk(Qt.Window, title="probe-parent")
    p.show()
    QTimer.singleShot(800, lambda: mk(MENU, parent=p).show())
elif variant == "modal-dialog-with-parent":
    p = mk(Qt.Window, title="probe-parent")
    p.show()

    def dlg():
        d = mk(Qt.Dialog | Qt.FramelessWindowHint, parent=p, cls=QDialog)
        d.setModal(True)
        d.show()
    QTimer.singleShot(800, dlg)
elif variant == "popup-with-parent":
    p = mk(Qt.Window, title="probe-parent")
    p.show()
    QTimer.singleShot(800, lambda: mk(Qt.Popup | Qt.FramelessWindowHint, parent=p).show())
print("ready", flush=True)
sys.exit(app.exec())
