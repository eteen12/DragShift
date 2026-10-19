// DragShift test helper (TEST ONLY).
//
// Loaded only inside the isolated headless session from tests/linux/session.py, which
// copies this directory into the session's temporary XDG_DATA_HOME. It is never shipped
// by an installer and must never be installed on a real desktop: it lets any client on
// the session bus list windows and take screenshots.
//
// D-Bus: io.github.eteen12.DragShiftTest at /io/github/eteen12/DragShiftTest.
// GNOME 46 refuses org.gnome.Shell.Screenshot to callers that are not allowlisted, so
// tests take screenshots through this extension instead.
//
// Input injection uses Clutter virtual input devices of this shell's own seat. They are
// in-process: no uinput or /dev/input device is created, so events only reach this
// shell. As a second line of defence they are only created when the shell is headless
// AND runs from a launcher session root; anywhere else the input methods return an error.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'io.github.eteen12.DragShiftTest';
const OBJECT_PATH = '/io/github/eteen12/DragShiftTest';
const SESSION_ROOT_PREFIX = 'dragshift-session-';
// Longest an input call waits for its events to be handled before it replies anyway.
const FLUSH_TIMEOUT_MS = 1000;

const IFACE = `
<node>
  <interface name="io.github.eteen12.DragShiftTest">
    <method name="Version">
      <arg type="s" direction="out" name="version"/>
    </method>
    <method name="Screenshot">
      <arg type="s" direction="in" name="path"/>
    </method>
    <method name="ScreenshotArea">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="path"/>
    </method>
    <method name="ListWindows">
      <arg type="aa{sv}" direction="out" name="windows"/>
    </method>
    <method name="RunningApps">
      <arg type="as" direction="out" name="apps"/>
    </method>
    <method name="ActivateWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="MoveWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="GetPointer">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="u" direction="out" name="mask"/>
    </method>
    <method name="GetWorkArea">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="(iiii)" direction="out" name="monitor"/>
      <arg type="(iiii)" direction="out" name="work_area"/>
    </method>
    <method name="MovePointer">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
    </method>
    <method name="Button">
      <arg type="u" direction="in" name="button"/>
      <arg type="b" direction="in" name="pressed"/>
    </method>
    <method name="Key">
      <arg type="u" direction="in" name="keyval"/>
      <arg type="b" direction="in" name="pressed"/>
    </method>
    <method name="TypeText">
      <arg type="s" direction="in" name="text"/>
    </method>
    <method name="ReleaseAll">
    </method>
  </interface>
</node>`;

const WINDOW_TYPES = Object.fromEntries(
    Object.entries(Meta.WindowType).map(([name, value]) => [value, name.toLowerCase()]));

// The launcher's session root (<tmp>/dragshift-session-XXXX) holds the session's
// XDG_DATA_HOME, HOME and TMPDIR. Screenshots may only be written below it.
function sessionRoot() {
    const root = GLib.path_get_dirname(GLib.get_user_data_dir());
    return GLib.path_get_basename(root).startsWith(SESSION_ROOT_PREFIX) ? root : null;
}

function checkedPath(path) {
    const root = sessionRoot();
    if (!root)
        throw new Error('not running in a DragShift test session; refusing to write files');
    if (!GLib.path_is_absolute(path))
        throw new Error(`path must be absolute: ${path}`);
    const canonical = GLib.canonicalize_filename(path, null);
    if (!canonical.startsWith(`${root}/`))
        throw new Error(`path must be inside the session root ${root}: ${path}`);
    return canonical;
}

// X11 window id, or 0 for Wayland clients. Meta.Window.get_xwindow() is not introspected
// in mutter 46 (Meta-14); for X11 windows get_description() is the id as "0x<hex>".
function xid(window) {
    if (window.get_client_type() !== Meta.WindowClientType.X11)
        return 0;
    const m = /^0x([0-9a-f]+)/i.exec(window.get_description() ?? '');
    return m ? parseInt(m[1], 16) : 0;
}

// A window by ListWindows "id" (Meta.Window.get_id()) or by X11 window id.
function findWindow(id) {
    const windows = global.get_window_actors().map(a => a.get_meta_window());
    return windows.find(win => win.get_id() === id) ??
        windows.find(win => id !== 0 && xid(win) === id);
}

function describe(actor, stacking, focus, tabList) {
    const w = actor.get_meta_window();
    const r = w.get_frame_rect();
    const x11 = w.get_client_type() === Meta.WindowClientType.X11;
    return {
        id: new GLib.Variant('t', w.get_id()),
        title: new GLib.Variant('s', w.get_title() ?? ''),
        wm_class: new GLib.Variant('s', w.get_wm_class() ?? ''),
        pid: new GLib.Variant('i', w.get_pid()),
        xid: new GLib.Variant('t', xid(w)),
        client: new GLib.Variant('s', x11 ? 'x11' : 'wayland'),
        frame: new GLib.Variant('(iiii)', [r.x, r.y, r.width, r.height]),
        above: new GLib.Variant('b', w.is_above()),
        focused: new GLib.Variant('b', w === focus),
        minimized: new GLib.Variant('b', w.minimized),
        stacking: new GLib.Variant('u', stacking),
        window_type: new GLib.Variant('s', WINDOW_TYPES[w.get_window_type()] ?? 'unknown'),
        // The shell's app for the window: '<desktop file id>' when one matches its
        // app_id / WM_CLASS, else a 'window:N' stand-in (what the dock shows).
        app: new GLib.Variant('s', Shell.WindowTracker.get_default().get_window_app(w)?.get_id() ?? ''),
        // Alt+Tab's list (Meta.TabList.NORMAL, all workspaces) and the dock's filter.
        in_tab_list: new GLib.Variant('b', tabList.includes(w)),
        skip_taskbar: new GLib.Variant('b', w.is_skip_taskbar()),
    };
}

// Keysym for a character, like Gdk.unicode_to_keyval: Latin-1 keysyms equal the code
// point, everything else is 0x01000000 + code point. Mutter types a keysym only when the
// session's keymap has a keycode for it (the default "us" layout covers ASCII).
const CONTROL_KEYSYMS = {'\n': 0xff0d, '\r': 0xff0d, '\t': 0xff09, '\b': 0xff08};

function keyvalFor(ch) {
    if (ch in CONTROL_KEYSYMS)
        return CONTROL_KEYSYMS[ch];
    const cp = ch.codePointAt(0);
    if ((cp >= 0x20 && cp <= 0x7e) || (cp >= 0xa0 && cp <= 0xff))
        return cp;
    return 0x01000000 + cp;
}

// Seat modifier-mask bits that pressing a key or button sets (Clutter.ModifierType):
// an input call waits until the seat's state shows them, see Input.settled().
const M = Clutter.ModifierType;
const KEY_BITS = {
    0xffe1: M.SHIFT_MASK, 0xffe2: M.SHIFT_MASK, // Shift_L, Shift_R
    0xffe3: M.CONTROL_MASK, 0xffe4: M.CONTROL_MASK, // Control_L, Control_R
    0xffe9: M.MOD1_MASK, 0xffea: M.MOD1_MASK, // Alt_L, Alt_R
};
const BUTTON_BITS = {1: M.BUTTON1_MASK, 2: M.BUTTON2_MASK, 3: M.BUTTON3_MASK};
const TRACKED_BITS = [...Object.values(KEY_BITS), ...Object.values(BUTTON_BITS)]
    .reduce((a, b) => a | b, 0);

// Virtual pointer and keyboard on this shell's own Clutter seat. Remembers what is held
// so release() can let go of everything (a failed test must not leave Shift down).
class Input {
    constructor() {
        const seat = Clutter.get_default_backend().get_default_seat();
        this._pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._buttons = new Set();
        this._keys = new Set();
    }

    move(x, y) {
        this._pointer.notify_absolute_motion(Clutter.CURRENT_TIME, x, y);
    }

    button(button, pressed) {
        this._pointer.notify_button(Clutter.CURRENT_TIME, button,
            pressed ? Clutter.ButtonState.PRESSED : Clutter.ButtonState.RELEASED);
        if (pressed)
            this._buttons.add(button);
        else
            this._buttons.delete(button);
    }

    key(keyval, pressed) {
        this._keyboard.notify_keyval(Clutter.CURRENT_TIME, keyval,
            pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        if (pressed)
            this._keys.add(keyval);
        else
            this._keys.delete(keyval);
    }

    // Whether the seat's modifier mask has caught up with what is held: Mutter's input
    // thread updates it asynchronously, and under load a stage update can come first.
    // Without this a key release could still show as held to the next caller (#106).
    settled() {
        let want = 0;
        for (const keyval of this._keys)
            want |= KEY_BITS[keyval] ?? 0;
        for (const button of this._buttons)
            want |= BUTTON_BITS[button] ?? 0;
        return (global.get_pointer()[2] & TRACKED_BITS) === want;
    }

    release() {
        for (const keyval of [...this._keys].reverse())
            this.key(keyval, false);
        for (const button of [...this._buttons])
            this.button(button, false);
    }

    // Disposing a virtual device removes it from the seat right away (not at GC).
    destroy() {
        this.release();
        this._pointer.run_dispose();
        this._keyboard.run_dispose();
        this._pointer = null;
        this._keyboard = null;
    }
}

class RefusedError extends Error {}

// Why input injection is refused here, or null when it is allowed: only in a headless
// shell started by the test launcher (tests/linux/session.py), never on a real desktop.
function inputRefusal() {
    if (!global.backend.is_headless())
        return 'the shell is not headless';
    if (!sessionRoot())
        return 'not running in a DragShift test session';
    return null;
}

class Helper {
    constructor(version, input, refusal) {
        this._version = version;
        this._input = input;
        this._refusal = refusal;
    }

    _checkInput() {
        if (!this._input)
            throw new RefusedError(`input injection refused: ${this._refusal}`);
        return this._input;
    }

    Version() {
        return this._version;
    }

    ScreenshotAsync([path], invocation) {
        this._shoot(invocation, path,
            (shooter, stream, done) => shooter.screenshot(false, stream, done),
            (shooter, res) => shooter.screenshot_finish(res));
    }

    ScreenshotAreaAsync([x, y, width, height, path], invocation) {
        if (width <= 0 || height <= 0) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs',
                `empty area ${width}x${height}`);
            return;
        }
        this._shoot(invocation, path,
            (shooter, stream, done) => shooter.screenshot_area(x, y, width, height, stream, done),
            (shooter, res) => shooter.screenshot_area_finish(res));
    }

    // Whole stage, no cursor, written as PNG.
    _shoot(invocation, path, start, finish) {
        let stream;
        try {
            stream = Gio.File.new_for_path(checkedPath(path))
                .replace(null, false, Gio.FileCreateFlags.NONE, null);
        } catch (e) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs', e.message);
            return;
        }
        const shooter = new Shell.Screenshot();
        start(shooter, stream, (obj, res) => {
            try {
                finish(obj, res);
                stream.close(null);
                invocation.return_value(null);
            } catch (e) {
                try {
                    stream.close(null);
                } catch {}
                invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', e.message);
            }
        });
    }

    // Bottom to top, in stacking order (the order of global.get_window_actors()).
    ListWindows() {
        const focus = global.display.focus_window;
        const tabList = global.display.get_tab_list(Meta.TabList.NORMAL, null);
        return global.get_window_actors().map((actor, i) => describe(actor, i, focus, tabList));
    }

    // Ids of the apps the shell counts as running (what the dock shows with a dot).
    RunningApps() {
        return Shell.AppSystem.get_default().get_running().map(app => app.get_id());
    }

    // id as for findWindow(). Main.activateWindow is the shell's own path (dash,
    // Alt+Tab): Meta.Window.activate() plus switching workspace and leaving the overview
    // if needed.
    ActivateWindow(id) {
        const w = findWindow(id);
        if (!w)
            return false;
        Main.activateWindow(w, global.get_current_time());
        return true;
    }

    // Puts a window's frame (the visible window without client-side shadows, as in
    // ListWindows "frame") at (x, y) with the given size, leaving maximized or
    // fullscreen state first. Mutter may still constrain the result (minimum size,
    // keeping the title bar on screen); the move is asynchronous for Wayland clients, so
    // callers poll ListWindows for the final frame (ShellHelper.move_window does).
    MoveWindow(id, x, y, width, height) {
        const w = findWindow(id);
        if (!w)
            return false;
        if (w.is_fullscreen())
            w.unmake_fullscreen();
        if (w.get_maximized())
            w.unmaximize(Meta.MaximizeFlags.BOTH);
        w.move_resize_frame(false, x, y, width, height);
        return true;
    }

    // The monitor that contains (x, y) (else the primary one) and Mutter's work area of
    // it on the active workspace (#42: toasts must stay inside it), stage pixels.
    GetWorkArea(x, y) {
        const display = global.display;
        let index = -1;
        for (let i = 0; i < display.get_n_monitors(); i++) {
            const m = display.get_monitor_geometry(i);
            if (x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height)
                index = i;
        }
        if (index < 0)
            index = display.get_primary_monitor();
        const m = display.get_monitor_geometry(index);
        const a = global.workspace_manager.get_active_workspace().get_work_area_for_monitor(index);
        return [[m.x, m.y, m.width, m.height], [a.x, a.y, a.width, a.height]];
    }

    GetPointer() {
        const [x, y, mask] = global.get_pointer();
        return [x, y, mask];
    }

    // Every input method returns only after the shell has handled its events. The seat
    // processes virtual events on Mutter's input thread, and the stage delivers motion
    // to clients on its next frame while keys and buttons go out at once. Without this a
    // key release sent right after a move could reach the client before the move.

    // Absolute motion in stage coordinates. Waits until the seat's pointer has reached
    // the target (clamped to the stage, as Mutter clamps it), then for the stage to
    // dispatch it. Returns the pointer position.
    MovePointerAsync([x, y], invocation) {
        this._inject(invocation, input => {
            input.move(x, y);
            return [Math.max(0, Math.min(x, global.stage.width - 1)),
                Math.max(0, Math.min(y, global.stage.height - 1))];
        }, () => new GLib.Variant('(ii)', global.get_pointer().slice(0, 2)));
    }

    // button: 1 primary, 2 middle, 3 secondary (Clutter numbering).
    ButtonAsync([button, pressed], invocation) {
        this._inject(invocation, input => input.button(button, pressed));
    }

    // keyval: an X keysym (Shift_L 0xffe1, Escape 0xff1b, Return 0xff0d, 'a' 0x61, ...).
    // A pressed key stays down until it is released explicitly.
    KeyAsync([keyval, pressed], invocation) {
        this._inject(invocation, input => input.key(keyval, pressed));
    }

    TypeTextAsync([text], invocation) {
        this._inject(invocation, input => {
            for (const ch of text) {
                const keyval = keyvalFor(ch);
                input.key(keyval, true);
                input.key(keyval, false);
            }
        });
    }

    // Releases every key and button pressed through this helper.
    ReleaseAllAsync(_params, invocation) {
        this._inject(invocation, input => input.release());
    }

    // Runs send(input); if it returns a target [x, y], first waits for the seat's pointer
    // to get there, and always until the seat's modifier mask matches the held keys and
    // buttons. Then waits for two stage updates, so any event the input thread handed to
    // the main loop meanwhile has been dispatched, and replies with reply().
    _inject(invocation, send, reply = () => null) {
        let target, input;
        try {
            input = this._checkInput();
            target = send(input);
        } catch (e) {
            invocation.return_dbus_error(e instanceof RefusedError
                ? 'org.freedesktop.DBus.Error.AccessDenied'
                : 'org.freedesktop.DBus.Error.Failed', e.message);
            return;
        }
        const stage = global.stage;
        const deadline = GLib.get_monotonic_time() + FLUSH_TIMEOUT_MS * 1000;
        let updates = 0, updateId = 0, timeoutId = 0;
        const done = () => {
            if (updateId)
                stage.disconnect(updateId);
            if (timeoutId)
                GLib.source_remove(timeoutId);
            updateId = timeoutId = 0;
            invocation.return_value(reply());
        };
        const flush = () => {
            updateId = stage.connect('after-update', () => {
                if (++updates >= 2)
                    done();
                else
                    stage.schedule_update();
            });
            stage.schedule_update();
        };
        const reached = () => {
            const [px, py] = global.get_pointer();
            return (!target || (px === target[0] && py === target[1])) && input.settled();
        };
        // Fallback so a call never hangs: reply anyway after FLUSH_TIMEOUT_MS.
        timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1, () => {
            if (GLib.get_monotonic_time() > deadline) {
                timeoutId = 0;
                done();
                return GLib.SOURCE_REMOVE;
            }
            if (!updateId && reached())
                flush();
            return GLib.SOURCE_CONTINUE;
        });
    }

}

export default class DragShiftTestExtension extends Extension {
    _trace(what) {
        console.log(`${this.uuid}: ${what} (overview visible=${Main.overview.visible} ` +
            `animating=${Main.overview.animationInProgress} startingUp=${Main.layoutManager._startingUp})`);
    }

    enable() {
        this._trace('enable');
        const refusal = inputRefusal();
        this._input = refusal ? null : new Input();
        if (refusal)
            console.warn(`${this.uuid}: input injection disabled: ${refusal}`);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(
            IFACE, new Helper(String(this.metadata.version), this._input, refusal));
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        // The headless shell boots into the overview (Ubuntu Dock's "no overview on
        // startup" does not take effect there), unlike a real Ubuntu login. While it is
        // up nothing has focus, and when it closes it hands the focus back to the window
        // that had it before, undoing an ActivateWindow made meanwhile. So leave it once
        // startup is done, and only then take the bus name: when Version answers, the
        // session shows the desktop. User extensions are enabled either during or just
        // after the startup animation, so handle both.
        if (Main.layoutManager._startingUp) {
            this._startupId = Main.layoutManager.connect('startup-complete', () => {
                Main.layoutManager.disconnect(this._startupId);
                this._startupId = 0;
                this._trace('startup-complete');
                this._leaveOverview();
            });
        } else {
            this._leaveOverview();
        }
    }

    _leaveOverview() {
        if (!Main.overview.visible) {
            this._ownName();
            return;
        }
        this._hiddenId = Main.overview.connect('hidden', () => {
            Main.overview.disconnect(this._hiddenId);
            this._hiddenId = 0;
            this._trace('overview hidden');
            this._ownName();
        });
        this._trace('hiding the overview');
        Main.overview.hide();
    }

    _ownName() {
        this._ownerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            () => this._trace(`owns ${BUS_NAME}`),
            () => this._trace(`lost ${BUS_NAME}`));
    }

    disable() {
        if (this._startupId) {
            Main.layoutManager.disconnect(this._startupId);
            this._startupId = 0;
        }
        if (this._hiddenId) {
            Main.overview.disconnect(this._hiddenId);
            this._hiddenId = 0;
        }
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = 0;
        }
        this._dbus?.unexport();
        this._dbus = null;
        this._input?.destroy();
        this._input = null;
    }
}
