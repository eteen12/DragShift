// Window placement for DragShift's own windows (#10: the app runs as a native Wayland
// client, which can neither place nor raise its windows). Serves PlaceWindow in dbus.xml.
//
// A rule says: the caller's window titled T goes with its frame's top-left at (x, y).
// Rules are kept per caller pid (asked from the bus, so an app can only move its own
// windows) and dropped when the caller leaves the bus. A rule is applied
// - at once to a mapped window of that pid and title (e.g. a toast sliding up), and
// - to a new window of that pid and title in its `shown` handler, which runs after
//   Mutter's own placement but before the window's first frame, so it never shows
//   anywhere else. Moving a window from `window-created` instead crashes Mutter 46
//   (measured in #10), so placement waits for `shown`.
// A placed window is kept above other windows, opens and closes without the shell's
// map/destroy animation (the app animates itself), and gives the keyboard focus straight
// back to the window that had it: Mutter focuses a newly mapped Wayland window and the
// target of a drop, and DragShift's menu and toasts must never keep the focus.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// Most titles one caller may keep rules for (the menu plus four toasts need five).
const MAX_RULES = 32;

export class WindowPlacer {
    constructor() {
        this._rules = new Map();      // pid -> Map(title -> [x, y])
        this._callers = new Map();    // unique bus name -> {pid, watchId}
        this._windows = new Map();    // window of a caller -> {placed, pending, ids}
        this._lastFocus = null;       // last focused window that is not one of ours
        this._createdId = global.display.connect('window-created',
            (_display, window) => this._onCreated(window));
        this._focusId = global.display.connect('notify::focus-window',
            () => this._onFocus());
        this._onFocus();
    }

    destroy() {
        global.display.disconnect(this._createdId);
        global.display.disconnect(this._focusId);
        for (const [window, {ids}] of this._windows)
            ids.forEach(id => window.disconnect(id));
        for (const {watchId} of this._callers.values())
            Gio.bus_unwatch_name(watchId);
        this._windows.clear();
        this._callers.clear();
        this._rules.clear();
        this._lastFocus = null;
    }

    // PlaceWindow(title, x, y) from `sender`; reply(placedNow) or fail(error).
    place(sender, title, x, y, reply, fail) {
        this._pidOf(sender, pid => {
            let rules = this._rules.get(pid);
            if (!rules) {
                rules = new Map();
                this._rules.set(pid, rules);
            }
            rules.delete(title);
            if (rules.size >= MAX_RULES)
                rules.delete(rules.keys().next().value);
            rules.set(title, [x, y]);
            let placed = false;
            for (const actor of global.get_window_actors()) {
                const window = actor.get_meta_window();
                // Only windows already on screen: moving one before it has been shown is
                // what crashes Mutter (see the top of this file). One that is still on its
                // way picks the rule up in its `shown` handler.
                if (window.get_pid() !== pid || window.get_title() !== title ||
                    !actor.is_mapped() || this._windows.get(window)?.pending)
                    continue;
                this._track(window).placed = true;
                this._apply(window, x, y);
                placed = true;
            }
            reply(placed);
        }, fail);
    }

    _pidOf(sender, done, fail) {
        const known = this._callers.get(sender);
        if (known) {
            done(known.pid);
            return;
        }
        Gio.DBus.session.call('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
            new GLib.Variant('(s)', [sender]), new GLib.VariantType('(u)'),
            Gio.DBusCallFlags.NONE, 1000, null, (conn, res) => {
                let pid;
                try {
                    [pid] = conn.call_finish(res).deepUnpack();
                } catch (e) {
                    fail(e);
                    return;
                }
                if (!this._callers.has(sender)) {
                    const watchId = Gio.bus_watch_name_on_connection(Gio.DBus.session,
                        sender, Gio.BusNameWatcherFlags.NONE, null,
                        () => this._forget(sender));
                    this._callers.set(sender, {pid, watchId});
                }
                done(pid);
            });
    }

    _forget(sender) {
        const caller = this._callers.get(sender);
        if (!caller)
            return;
        Gio.bus_unwatch_name(caller.watchId);
        this._callers.delete(sender);
        if (![...this._callers.values()].some(c => c.pid === caller.pid))
            this._rules.delete(caller.pid);
    }

    // Book-keeping for a window of a caller: its signal ids, whether a rule placed it and
    // whether it is still waiting to be shown. Dropped when the window goes away.
    _track(window) {
        let entry = this._windows.get(window);
        if (entry)
            return entry;
        entry = {placed: false, pending: false, ids: []};
        this._windows.set(window, entry);
        entry.ids.push(window.connect('unmanaging', () => {
            const actor = window.get_compositor_private();
            if (entry.placed && actor)
                Main.wm.skipNextEffect(actor);
            entry.ids.forEach(id => window.disconnect(id));
            this._windows.delete(window);
            if (this._lastFocus === window)
                this._lastFocus = null;
        }));
        return entry;
    }

    _onCreated(window) {
        const pid = window.get_pid();
        if (!this._rules.has(pid))
            return;
        // The title is not known yet, so every new window of a caller with rules skips
        // the map animation (it must be skipped now; in `shown` it has already started).
        const actor = window.get_compositor_private();
        if (actor)
            Main.wm.skipNextEffect(actor);
        const entry = this._track(window);
        entry.pending = true;
        const shownId = window.connect('shown', () => {
            window.disconnect(shownId);
            entry.ids = entry.ids.filter(id => id !== shownId);
            entry.pending = false;
            const xy = this._rules.get(pid)?.get(window.get_title());
            if (xy) {
                entry.placed = true;
                this._apply(window, ...xy);
            } else if (global.display.focus_window === window) {
                this._lastFocus = window;   // not a placed window (e.g. Settings)
            }
        });
        entry.ids.push(shownId);
    }

    _apply(window, x, y) {
        const r = window.get_frame_rect();
        if (r.x !== x || r.y !== y)
            window.move_frame(false, x, y);
        if (!window.is_above())
            window.make_above();
        if (global.display.focus_window === window)
            this._giveBackFocus();
    }

    _onFocus() {
        const focus = global.display.focus_window;
        if (!focus)
            return;
        const entry = this._windows.get(focus);
        if (entry?.placed)
            this._giveBackFocus();
        else if (!entry?.pending)
            this._lastFocus = focus;
    }

    _giveBackFocus() {
        const previous = this._lastFocus;
        if (previous && previous.get_compositor_private())
            previous.focus(global.get_current_time());
    }
}
