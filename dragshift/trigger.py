"""Watches for "left button held + mouse moved + Shift pressed" anywhere on screen, which
is what a Shift+drag of a file looks like. Polling global input state is simple, needs no
admin rights and can't stall the system the way a broken hook can.
Windows: GetAsyncKeyState. macOS: NSEvent.pressedMouseButtons / modifierFlags (no
Accessibility or Input Monitoring permission required). Linux: platform_linux.pointer_state()
(from the GNOME Shell extension), which also supplies the pointer position."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QCursor

from .util import linux_module

IS_LINUX = sys.platform.startswith("linux")
VK_LBUTTON, VK_SHIFT, VK_ESCAPE = 0x01, 0x10, 0x1B
DRAG_THRESHOLD = 10   # px the mouse must travel with the button held
NO_DRAG_TIMEOUT = 50  # ticks (≈ 0.75 s) to wait for a file drag to enter before hiding


class DragShiftWatcher(QObject):
    show_menu = Signal(QPoint)
    toggle_ring = Signal()
    hide_menu = Signal()

    def __init__(self, menu_visible, menu_got_drag, parent=None):
        super().__init__(parent)
        self.menu_visible = menu_visible
        self.menu_got_drag = menu_got_drag
        self.enabled = True
        self.vk = VK_SHIFT
        self.key_name = "shift"
        self._down_at: QPoint | None = None
        self._shift_was = False
        self._fired = False
        self._idle_ticks = 0
        self._shown_at = QPoint()
        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None
        self._mac = None
        if sys.platform == "darwin":
            from . import platform_mac
            self._mac = platform_mac if platform_mac.OK else None
        self._linux = linux_module() if IS_LINUX else None
        self._pos = QPoint()   # Linux: pointer position from the last pointer_state()
        self.timer = QTimer(self, interval=15, timeout=self._tick)

    def set_key(self, name: str):
        self.key_name = name
        self.vk = {"shift": VK_SHIFT, "ctrl": 0x11, "alt": 0x12}.get(name, VK_SHIFT)

    def start(self):
        if self._user32 is not None or self._mac is not None:
            self.timer.start()
        elif self._linux is not None and self._linux.extension_available():
            # no pointer data without the extension: stay idle instead of polling nothing
            self.timer.start()

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _state(self) -> tuple[bool, bool, bool] | None:
        """(left button, trigger key, escape); None = no input state this tick (Linux)"""
        if self._mac is not None:
            m = self._mac
            return m.left_button_down(), m.key_down(self.key_name), m.escape_down()
        if self._linux is not None:
            st = self._linux.pointer_state()
            if st is None:
                return None
            x, y, left, mask = st
            self._pos = QPoint(int(x), int(y))
            key = self._linux.KEY_MASKS.get(self.key_name, self._linux.SHIFT_MASK)
            return bool(left), bool(mask & key), False   # Escape: not reported yet
        return self._down(VK_LBUTTON), self._down(self.vk), self._down(VK_ESCAPE)

    def _tick(self):
        state = self._state()
        if state is None:
            return
        lbtn, shift, esc = state
        pos = self._pos if self._linux is not None else QCursor.pos()

        if self.menu_visible():
            if not lbtn or esc:
                # drop happened (or was cancelled) – give the drop event a moment to land
                QTimer.singleShot(120, self.hide_menu.emit)
            elif shift and not self._shift_was and self.menu_got_drag():
                self.toggle_ring.emit()       # tap Shift again to flip Convert ⇄ Tools
            elif not self.menu_got_drag() and (pos - self._shown_at).manhattanLength() > 6:
                # the cursor is moving over us but no file drag arrived: it's a window
                # move or text selection, so get out of the way
                self._idle_ticks += 1
                if self._idle_ticks > NO_DRAG_TIMEOUT:
                    self.hide_menu.emit()
        else:
            if lbtn:
                if self._down_at is None:
                    self._down_at = pos
                    self._fired = False
                moved = (pos - self._down_at).manhattanLength() >= DRAG_THRESHOLD
                if self.enabled and moved and shift and not self._fired:
                    self._fired = True
                    self._idle_ticks = 0
                    self._shown_at = pos
                    self.show_menu.emit(pos)
            else:
                self._down_at = None
                self._fired = False
        self._shift_was = shift
