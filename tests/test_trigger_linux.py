"""DragShiftWatcher on Linux (#31): fed by platform_linux.pointer_state() sequences, with
the shell extension faked, so no D-Bus or GNOME session is needed.

pointer_state() gives (x, y, left_down, Clutter modifier mask) or None (no data)."""
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux watcher")

SHIFT, CTRL, ALT = 1 << 0, 1 << 2, 1 << 3


class Clock:
    """Stands in for trigger.time: monotonic() returns `now`, moved by hand."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class NoCursor:
    """QCursor must never be read on Linux (stale under XWayland, empty under Wayland)."""
    @staticmethod
    def pos():
        raise AssertionError("QCursor.pos() called on Linux")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ext(monkeypatch):
    """The extension as the watcher sees it: `ext.feed(...)` queues pointer states (None =
    no data), `ext.up` is what extension_available() answers."""
    from dragshift import platform_linux as lx

    class Ext:
        up = True
        queue: list = []

        def feed(self, *states):
            self.queue.extend(states)

    e = Ext()
    e.queue = []
    monkeypatch.setattr(lx, "pointer_state", lambda: e.queue.pop(0))
    monkeypatch.setattr(lx, "extension_available", lambda: e.up)
    return e


@pytest.fixture
def watcher(qapp, ext, monkeypatch):
    from dragshift import trigger
    monkeypatch.setattr(trigger, "QCursor", NoCursor)
    clock = Clock()
    monkeypatch.setattr(trigger, "time", clock)
    from PySide6.QtCore import QRect
    # left_at: when the file drag left the menu (None: over it); rect: the menu's frame
    menu = {"visible": False, "got_drag": False, "left_at": None, "rect": QRect(0, 0, 400, 400)}
    w = trigger.DragShiftWatcher(
        lambda: menu["visible"], lambda: menu["got_drag"],
        menu_drag_left=lambda pos: menu["left_at"] if menu["rect"].contains(pos) else None)
    w.menu, w.clock, w.events = menu, clock, []
    w.show_menu.connect(lambda p: w.events.append(("show", p.x(), p.y())))
    w.toggle_ring.connect(lambda: w.events.append(("toggle",)))
    w.hide_menu.connect(lambda: w.events.append(("hide",)))

    def run(*states):
        ext.feed(*states)
        for _ in states:
            w._tick()
        return w.events
    w.run = run
    yield w
    w.timer.stop()
    w._probe.stop()


def _wait_for(w, event, timeout=10.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while event not in w.events and time.monotonic() < deadline:
        QTest.qWait(10)
    return event in w.events


# ---------------------------------------------------------------- opening the menu
def test_shift_after_moving_opens_at_the_pointer(watcher):
    ev = watcher.run((100, 100, True, 0), (130, 100, True, 0),    # dragging, no Shift yet
                     (160, 110, True, SHIFT))                     # Shift pressed mid-drag
    assert ev == [("show", 160, 110)]


def test_shift_before_the_button_opens(watcher):
    ev = watcher.run((100, 100, False, SHIFT),                    # Shift held, then press
                     (100, 100, True, SHIFT), (105, 100, True, SHIFT),
                     (120, 100, True, SHIFT))                     # 20 px: past the threshold
    assert ev == [("show", 120, 100)]


def test_linux_waits_for_a_longer_move(watcher):
    """20 px instead of Windows/macOS's 10, so the file manager has started its drag
    before the menu maps (DING with Shift held at the press starts none otherwise)."""
    from dragshift import trigger
    assert trigger.LINUX_DRAG_THRESHOLD == 20 and trigger.DRAG_THRESHOLD == 10
    assert watcher.run((100, 100, True, SHIFT), (112, 100, True, SHIFT),
                       (110, 109, True, SHIFT)) == []             # 12 and 19 px
    assert watcher.run((110, 110, True, SHIFT)) == [("show", 110, 110)]


def test_drag_without_the_key_opens_nothing(watcher):
    assert watcher.run(*[(100 + 10 * i, 100, True, 0) for i in range(20)]) == []


def test_other_modifier_bits_are_ignored(watcher):
    num_lock, super_ = 1 << 4, 1 << 26
    assert watcher.run((100, 100, True, num_lock | super_ | 1 << 8),
                       (130, 100, True, num_lock | super_)) == []
    assert watcher.run((140, 100, True, num_lock | SHIFT)) == [("show", 140, 100)]


def test_one_menu_per_drag(watcher):
    ev = watcher.run((100, 100, True, SHIFT), (120, 100, True, SHIFT), (200, 100, True, SHIFT),
                     (200, 100, False, SHIFT),                    # released: next drag may open
                     (300, 300, True, SHIFT), (320, 300, True, SHIFT))
    assert ev == [("show", 120, 100), ("show", 320, 300)]


def test_disabled_opens_nothing(watcher):
    watcher.enabled = False
    assert watcher.run((100, 100, True, SHIFT), (150, 100, True, SHIFT)) == []
    watcher.enabled = True
    assert watcher.run((160, 100, True, SHIFT)) == [("show", 160, 100)]


@pytest.mark.parametrize("key, mask", [("shift", SHIFT), ("ctrl", CTRL), ("alt", ALT)])
def test_trigger_key_maps_to_its_clutter_bit(watcher, key, mask):
    watcher.set_key(key)
    others = (SHIFT | CTRL | ALT) & ~mask
    assert watcher.run((100, 100, True, others), (150, 100, True, others)) == []
    assert watcher.run((160, 100, True, mask)) == [("show", 160, 100)]


def test_set_key_takes_effect_on_the_next_tick(watcher):
    assert watcher.run((100, 100, True, CTRL), (150, 100, True, CTRL)) == []
    watcher.set_key("ctrl")                    # changed in Settings mid-drag
    assert watcher.run((150, 100, True, CTRL)) == [("show", 150, 100)]


def test_shift_tap_over_the_menu_flips_the_ring(watcher):
    watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    watcher.menu.update(visible=True, got_drag=True)
    ev = watcher.run((140, 100, True, SHIFT), (150, 100, True, 0), (150, 100, True, SHIFT))
    assert ev == [("show", 130, 100), ("toggle",)]


def test_release_hides_after_the_drop_delay(watcher):
    watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    watcher.menu.update(visible=True, got_drag=True)
    watcher.run((130, 100, False, 0))
    assert ("hide",) not in watcher.events     # the drop gets 120 ms to land first
    assert _wait_for(watcher, ("hide",))


# ---------------------------------------------------------------- no pointer data
def test_none_with_the_menu_closed_skips_the_tick(watcher):
    """A gap in the data keeps the press point: the threshold still counts from it."""
    ev = watcher.run((100, 100, True, SHIFT), None, None, (116, 100, True, SHIFT))
    assert ev == []                            # 16 px from the press, not from the gap
    assert watcher.run((121, 100, True, SHIFT)) == [("show", 121, 100)]


def test_none_with_the_menu_open_counts_as_released(watcher):
    """#1's audit: an open menu must not stay up on stale state when the extension stops.
    It arrives as a run of Nones (the client's back-off)."""
    watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    watcher.menu.update(visible=True, got_drag=True)
    watcher.run(None, None, None)
    assert _wait_for(watcher, ("hide",))


def test_no_data_is_logged_at_most_once_a_minute(watcher, capsys):
    watcher.run(None)
    watcher.clock.now += 30
    watcher.run(None, (100, 100, False, 0), None)
    assert capsys.readouterr().err.count("no pointer state") == 1
    watcher.clock.now += 31
    watcher.run(None)
    assert capsys.readouterr().err.count("no pointer state") == 1


# ---------------------------------------------------------------- start and re-check
def test_start_polls_when_the_extension_answers(watcher, ext, capsys):
    watcher.start()
    assert watcher.timer.isActive() and not watcher._probe.isActive()
    assert "trigger: following the extension's pointer state" in capsys.readouterr().out


def test_extension_enabled_after_start_is_picked_up(watcher, ext):
    from dragshift import trigger
    ext.up = False
    watcher.start()
    assert not watcher.timer.isActive()        # no extension: no 15 ms polling
    assert watcher._probe.isActive() and watcher._probe.interval() == trigger.PROBE_MS
    watcher._probe_extension()                 # still missing on the next probe
    assert not watcher.timer.isActive()
    ext.up = True                              # enabled while the app runs
    watcher._probe_extension()
    assert watcher.timer.isActive() and not watcher._probe.isActive()
    assert watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT)) == [("show", 130, 100)]


def test_probe_timer_finds_the_extension_by_itself(watcher, ext):
    from PySide6.QtTest import QTest
    ext.up = False
    watcher._probe.setInterval(20)
    watcher.start()
    QTest.qWait(60)
    assert not watcher.timer.isActive()
    ext.up = True
    deadline = time.monotonic() + 10
    while not watcher.timer.isActive() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert watcher.timer.isActive() and not watcher._probe.isActive()


def test_lost_extension_goes_idle_then_comes_back(watcher, ext):
    from dragshift import trigger
    watcher.start()
    watcher.run((100, 100, False, 0), None)
    watcher.clock.now += trigger.LOST_AFTER - 0.1
    watcher.run(None)
    assert watcher.timer.isActive()            # a short gap (busy shell): keep polling
    watcher.clock.now += 0.2
    ext.up = False
    watcher.run(None)
    assert not watcher.timer.isActive() and watcher._probe.isActive()
    ext.up = True
    watcher._probe_extension()
    assert watcher.timer.isActive() and not watcher._probe.isActive()


def test_data_resets_the_lost_clock(watcher):
    from dragshift import trigger
    watcher.start()
    watcher.run(None)
    watcher.clock.now += trigger.LOST_AFTER - 1
    watcher.run((100, 100, False, 0))
    watcher.clock.now += 2
    watcher.run(None)
    assert watcher.timer.isActive()


def test_open_menu_never_goes_idle_before_closing(watcher):
    """While the menu is open a lost extension closes it first; idling waits for that."""
    from dragshift import trigger
    watcher.start()
    watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    watcher.menu.update(visible=True, got_drag=True)
    watcher.run(None)
    watcher.clock.now += trigger.LOST_AFTER + 1
    watcher.run(None)
    assert watcher.timer.isActive()
    assert _wait_for(watcher, ("hide",))


# ---------------------------------------------------------------- drag cancel and Escape (#32)
def _open_with_a_file(w):
    w.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    w.menu.update(visible=True, got_drag=True)
    w.events.clear()


def test_cancel_over_the_menu_closes_it_after_the_grace(watcher, capsys):
    """Escape mid-drag: Mutter cancels the drag and the menu gets a dragLeave with the
    button still down and the pointer still on it (#24). After DRAG_CANCEL_GRACE with no
    drop, the menu closes at once (the grace already waited for a drop)."""
    from dragshift import trigger
    _open_with_a_file(watcher)
    watcher.menu["left_at"] = watcher.clock.now
    assert watcher.run((140, 100, True, SHIFT)) == []
    watcher.clock.now += trigger.DRAG_CANCEL_GRACE - 0.005
    assert watcher.run((140, 100, True, SHIFT)) == []
    watcher.clock.now += 0.01
    assert watcher.run((140, 100, True, SHIFT)) == [("hide",)]      # no 120 ms delay
    assert "trigger: drag cancelled over the menu" in capsys.readouterr().out
    assert trigger.DRAG_CANCEL_GRACE == 0.15


def test_drag_coming_back_within_the_grace_keeps_the_menu(watcher):
    _open_with_a_file(watcher)
    watcher.menu["left_at"] = watcher.clock.now
    watcher.clock.now += 0.1
    watcher.menu["left_at"] = None                 # dragEnter again
    watcher.clock.now += 0.5
    assert watcher.run((140, 100, True, SHIFT), (150, 100, True, SHIFT)) == []


def test_drag_carried_out_of_the_menu_stays_until_the_button_goes_up(watcher):
    """A leave with the pointer outside the menu is the user dragging away, not a cancel:
    the menu stays (they may come back), as on Windows, and closes on release."""
    _open_with_a_file(watcher)
    watcher.menu["left_at"] = watcher.clock.now
    watcher.clock.now += 1.0
    assert watcher.run((450, 100, True, SHIFT), (600, 300, True, SHIFT)) == []
    watcher.run((600, 300, False, 0))
    assert ("hide",) not in watcher.events          # the ordinary drop delay
    assert _wait_for(watcher, ("hide",))


def test_release_after_a_leave_keeps_the_drop_delay(watcher):
    _open_with_a_file(watcher)
    watcher.menu["left_at"] = watcher.clock.now
    watcher.clock.now += 1.0
    watcher.run((140, 100, False, 0))
    assert ("hide",) not in watcher.events
    assert _wait_for(watcher, ("hide",))


def test_no_cancel_before_a_file_entered(watcher):
    watcher.run((100, 100, True, SHIFT), (130, 100, True, SHIFT))
    watcher.menu.update(visible=True, got_drag=False, left_at=watcher.clock.now)
    watcher.events.clear()
    watcher.clock.now += 1.0
    assert watcher.run((131, 100, True, SHIFT), (132, 101, True, SHIFT)) == []


def test_without_a_menu_hook_nothing_is_cancelled(qapp, ext, monkeypatch):
    """The two-argument watcher (as before #32): the button and the no-drag timeout only."""
    from dragshift import trigger
    monkeypatch.setattr(trigger, "QCursor", NoCursor)
    w = trigger.DragShiftWatcher(lambda: True, lambda: True)
    hides = []
    w.hide_menu.connect(lambda: hides.append(1))
    ext.feed(*[(140, 100, True, SHIFT)] * 5)
    for _ in range(5):
        w._tick()
    assert hides == []
    w.timer.stop()
    w._probe.stop()
