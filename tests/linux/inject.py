"""Pointer and keyboard input for tests in the isolated GNOME session (Linux only).

Builds mouse and keyboard gestures on the test-helper extension's `MovePointer`,
`Button`, `Key` and `TypeText` (tests/linux/shell.py). The extension injects through
Clutter virtual devices of the *isolated* shell's own seat, and refuses to do so unless
that shell is headless and was started by tests/linux/session.py, so nothing here can
reach the real desktop. Never replace it with uinput, ydotool or xdotool: those reach
the real seat (on this machine /dev/uinput is writable by the user).

Every helper call returns after the shell has handled its events, so gestures arrive in
the order they were sent.

Usage::

    from tests.linux import inject
    inject.glide(shell, 500, 400)
    inject.click(shell, 520, 410)
    with inject.hold_key(shell, "Shift_L"):
        inject.glide(shell, 700, 500)
    inject.drag_to(shell, 900, 600, start=(400, 300), hold="Shift_L")
    inject.type_text(shell, "abc")
"""
from __future__ import annotations

import contextlib
import time

# X keysyms by name (X11/keysymdef.h). Single characters map through keysym() below.
KEYSYMS = {
    "BackSpace": 0xff08, "Tab": 0xff09, "Return": 0xff0d, "Escape": 0xff1b,
    "Delete": 0xffff, "Home": 0xff50, "Left": 0xff51, "Up": 0xff52, "Right": 0xff53,
    "Down": 0xff54, "Page_Up": 0xff55, "Page_Down": 0xff56, "End": 0xff57,
    "Shift_L": 0xffe1, "Shift_R": 0xffe2, "Control_L": 0xffe3, "Control_R": 0xffe4,
    "Alt_L": 0xffe9, "Alt_R": 0xffea, "Super_L": 0xffeb, "Super_R": 0xffec,
    "space": 0x20, "F1": 0xffbe, "F10": 0xffc7,
}
BUTTONS = {"left": 1, "middle": 2, "right": 3}

GLIDE_STEPS = 60      # minimum steps per glide
DRAG_NUDGE = 12       # px moved right after the button goes down (crosses drag thresholds)


def keysym(key) -> int:
    """An X keysym from an int, a KEYSYMS name or a single character. Characters follow
    Gdk.unicode_to_keyval: Latin-1 keysyms equal the code point, others are
    0x01000000 + code point."""
    if isinstance(key, int):
        return key
    if key in KEYSYMS:
        return KEYSYMS[key]
    if len(key) == 1:
        cp = ord(key)
        return cp if 0x20 <= cp <= 0x7e or 0xa0 <= cp <= 0xff else 0x01000000 + cp
    raise ValueError(f"unknown key {key!r}; use a KEYSYMS name, a character or a keysym")


def _button(button) -> int:
    return BUTTONS[button] if isinstance(button, str) else int(button)


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def glide(shell, x: int, y: int, dur: float = 0.4, steps: int | None = None) -> tuple[int, int]:
    """Move from the current pointer position to (x, y) in at least GLIDE_STEPS steps
    eased with smoothstep, taking about `dur` seconds (longer if the shell is slower).
    Buttons and keys held meanwhile stay held. Returns the final pointer position."""
    x0, y0, _ = shell.get_pointer()
    steps = max(GLIDE_STEPS, steps or int(dur * 60))
    t0 = time.monotonic()
    pos = (x0, y0)
    for i in range(1, steps + 1):
        t = _smoothstep(i / steps)
        pos = shell.move_pointer(x0 + (x - x0) * t, y0 + (y - y0) * t)
        wait = t0 + dur * i / steps - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    return pos


def move(shell, x: int, y: int) -> tuple[int, int]:
    """Jump to (x, y) in one event."""
    return shell.move_pointer(x, y)


def button_down(shell, button="left") -> None:
    shell.button(_button(button), True)


def button_up(shell, button="left") -> None:
    shell.button(_button(button), False)


def click(shell, x: int | None = None, y: int | None = None, button="left",
          count: int = 1, dur: float = 0.2) -> None:
    """Glide to (x, y) if given, then press and release `button` `count` times."""
    if x is not None and y is not None:
        glide(shell, x, y, dur)
    b = _button(button)
    for i in range(count):
        if i:
            time.sleep(0.05)
        shell.button(b, True)
        shell.button(b, False)


def press(shell, key) -> None:
    """Tap a key: press and release (`key` as for keysym())."""
    k = keysym(key)
    shell.key(k, True)
    shell.key(k, False)


@contextlib.contextmanager
def hold_key(shell, *keys):
    """Hold keys (pressed in order) for the duration of the block; they stay down until
    the block ends, across any number of moves, and are released in reverse order even
    if the block raises."""
    held = []
    try:
        for key in keys:
            k = keysym(key)
            shell.key(k, True)
            held.append(k)
        yield
    finally:
        for k in reversed(held):
            shell.key(k, False)


def drag_to(shell, x: int, y: int, *, start: tuple[int, int] | None = None,
            dur: float = 0.6, button="left", hold=None, release: bool = True) -> None:
    """Drag to (x, y): glide to `start` if given, press `button`, nudge DRAG_NUDGE px
    towards the target (so toolkits start a drag), glide to (x, y) and release.

    hold: a key (or a tuple of keys) pressed after the nudge, held during the glide and
    the drop, and released after the button (DragShift's Shift+drag).
    release=False leaves the button (and `hold`) down; release them with button_up()
    and hold_key()/release_all().
    """
    if start is not None:
        glide(shell, *start, dur=0.25)
    x0, y0, _ = shell.get_pointer()
    b = _button(button)
    shell.button(b, True)
    time.sleep(0.15)
    dx, dy = x - x0, y - y0
    norm = max(1.0, (dx * dx + dy * dy) ** 0.5)
    for k in range(1, 7):
        shell.move_pointer(x0 + dx / norm * DRAG_NUDGE * k / 6,
                           y0 + dy / norm * DRAG_NUDGE * k / 6)
        time.sleep(0.02)
    keys = [] if hold is None else [keysym(k) for k in
                                     (hold if isinstance(hold, (tuple, list)) else (hold,))]
    try:
        for k in keys:
            shell.key(k, True)
        glide(shell, x, y, dur)
        time.sleep(0.1)
    except BaseException:
        shell.release_all()
        raise
    if release:
        shell.button(b, False)
        time.sleep(0.05)
        for k in reversed(keys):
            shell.key(k, False)


def type_text(shell, text: str) -> None:
    """Type `text` into the focused window (characters must be in the session's keymap,
    the default "us" layout; '\\n' is Return, '\\t' Tab, '\\b' BackSpace)."""
    shell.type_text(text)


def release_all(shell) -> None:
    """Release every key and button still held through the helper."""
    shell.release_all()
