// DragShift GNOME Shell extension: the D-Bus service the DragShift app asks for what
// Wayland withholds from ordinary clients. The interface is in dbus.xml.
//
// enable() exports the object and owns the bus name; disable() undoes both and drops the
// window-placement rules (placement.js). Both are idempotent. Nothing in the shell (Main,
// the stage) is patched.
//
// Every method but Version answers only the DragShift app (#93, caller.js): each goes
// through this._callers.serve(), which replies AccessDenied to anyone else.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {Callers, isTestSession} from './caller.js';
import {WindowPlacer} from './placement.js';

const BUS_NAME = 'io.github.eteen12.DragShift';
const OBJECT_PATH = '/io/github/eteen12/DragShift';
// CaptureArea: at most this many strips, about this many logical pixels each.
const CAPTURE_MAX_STRIPS = 4;
const CAPTURE_STRIP_PIXELS = 40000;

class DragShiftService {
    constructor(metadata, callers, placer) {
        this._version = metadata['version-name'] ?? 'unknown';
        this._callers = callers;
        this._placer = placer;
        this.escape = null;     // EscapeWatch, set by enable()
    }

    // Open to every caller: the app and the installer use it to find the extension.
    Version() {
        return this._version;
    }

    GetPointerAsync(_params, invocation) {
        this._callers.serve(invocation, () => {
            const [x, y, mask] = global.get_pointer();
            invocation.return_value(new GLib.Variant('(iiu)', [x, y, mask]));
        });
    }

    // Window placement (#10), see placement.js.
    PlaceWindowAsync([title, x, y], invocation) {
        this._callers.serve(invocation, pid => invocation.return_value(new GLib.Variant(
            '(b)', [this._placer.place(invocation.get_sender(), pid, title, x, y)])));
    }

    // See EscapeWatch below.
    WatchEscapeAsync([on], invocation) {
        this._callers.serve(invocation, () => invocation.return_value(new GLib.Variant(
            '(b)', [this.escape.watch(on, invocation.get_sender())])));
    }

    // ------------------------------------------------------------ CaptureArea (#26)
    // The screen under a rect without the drag icon. Mutter draws a Wayland drag's icon
    // in the compositor's feedback group; an X11 app's drag icon is an override-redirect
    // window of type DND. Both are hidden only around the offscreen paint:
    // Shell.Screenshot.screenshot_area paints the stage synchronously (no cursor) and
    // only encodes the PNG in a thread, so they are shown again in the same main-loop
    // iteration, before the next frame, and nothing flickers on screen.
    //
    // PNG encoding is nearly all of the cost (~40 ms for 400x400 of a photo), so the
    // rect is painted as up to CAPTURE_MAX_STRIPS horizontal strips whose PNGs are
    // encoded in parallel threads; the app stacks them.
    CaptureAreaAsync(params, invocation) {
        this._callers.serve(invocation,
            () => this._serveCapture(params, invocation, () => []));
    }

    // Validate and clamp the rect, run the capture after the previous one, reply
    // (x, y, width, height, strips) or a D-Bus error. extraHidden() gives more actors to
    // hide during the paint; it may return a Promise and runs in this capture's turn.
    _serveCapture([x, y, width, height], invocation, extraHidden) {
        if (width <= 0 || height <= 0) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs',
                `empty area ${width}x${height}`);
            return;
        }
        const x0 = Math.max(0, x), y0 = Math.max(0, y);
        const x1 = Math.min(Math.trunc(global.stage.width), x + width);
        const y1 = Math.min(Math.trunc(global.stage.height), y + height);
        if (x1 <= x0 || y1 <= y0) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.InvalidArgs',
                `area ${width}x${height}+${x}+${y} is off the stage`);
            return;
        }
        const ay = new GLib.VariantType('ay');
        // One capture at a time: the next starts once the previous PNGs are written.
        this._captures = (this._captures ?? Promise.resolve())
            .then(() => extraHidden())
            .then(extra => this._captureArea(x0, y0, x1 - x0, y1 - y0, extra))
            .then(strips => invocation.return_value(GLib.Variant.new_tuple([
                ...[x0, y0, x1 - x0, y1 - y0].map(v => new GLib.Variant('i', v)),
                GLib.Variant.new_array(ay,
                    strips.map(png => GLib.Variant.new_from_bytes(ay, png, true)))])))
            .catch(e => invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed',
                String(e?.message ?? e)));
    }

    // Promise of the PNGs (GLib.Bytes) of the rect's strips, top to bottom. `extra`:
    // more actors to hide during the paint (see CaptureAreaExcludingCaller).
    _captureArea(x, y, width, height, extra = []) {
        const n = Math.max(1, Math.min(CAPTURE_MAX_STRIPS, height,
            Math.round(width * height / CAPTURE_STRIP_PIXELS)));
        const shots = [];
        const hidden = [global.compositor.get_feedback_group(),
            ...global.get_window_actors().filter(a =>
                a.get_meta_window()?.get_window_type() === Meta.WindowType.DND), ...extra]
            .filter(a => a.visible);
        hidden.forEach(a => a.hide());
        try {
            for (let i = 0; i < n; i++) {
                const top = y + Math.round(height * i / n);
                const bottom = y + Math.round(height * (i + 1) / n);
                shots.push(new Promise((resolve, reject) => {
                    const stream = Gio.MemoryOutputStream.new_resizable();
                    new Shell.Screenshot().screenshot_area(x, top, width, bottom - top,
                        stream, (obj, res) => {
                            try {
                                obj.screenshot_area_finish(res);
                                stream.close(null);
                                resolve(stream.steal_as_bytes());
                            } catch (e) {
                                reject(e);
                            }
                        });
                }));
            }
        } finally {
            hidden.forEach(a => a.show());
        }
        return Promise.all(shots);
    }

    // ------------------------------------------------ CaptureAreaExcludingCaller (#28)
    // CaptureArea that also leaves out every window of the calling process, so the app
    // can re-capture what is behind its own visible toasts and tool windows. The caller
    // is identified by the bus, not by anything it sends: its pid from the bus daemon
    // (caller.js), matched against Meta.Window.get_pid() (Wayland client credentials, or
    // XRes for Xwayland windows). Those actors are hidden in the same synchronous paint
    // as the drag icon, so they never disappear from the screen.
    CaptureAreaExcludingCallerAsync(params, invocation) {
        this._callers.serve(invocation, pid =>
            this._serveCapture(params, invocation, () => this._windowActorsOf(pid)));
    }

    // Window actors whose window belongs to process `pid` (none for an unknown pid).
    _windowActorsOf(pid) {
        if (!(pid > 0))
            return [];
        return global.get_window_actors().filter(a => a.get_meta_window()?.get_pid() === pid);
    }

    // ------------------------------------------------------------ GetWorkArea (#42)
    // The work area (the monitor minus the top bar and a fixed dock) of the monitor that
    // contains (x, y), else of the primary monitor, in stage (logical) pixels. Toasts
    // stack at its bottom right. A native Wayland client can't learn it: Qt's
    // availableGeometry() is the whole monitor there. Read-only, no side effects.
    GetWorkAreaAsync([x, y], invocation) {
        const layout = Main.layoutManager;
        let index = layout.monitors.findIndex(m =>
            x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height);
        if (index < 0)
            index = layout.primaryIndex;
        if (index < 0) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', 'no monitor');
            return;
        }
        const r = layout.getWorkAreaForMonitor(index);
        invocation.return_value(new GLib.Variant('(iiii)', [r.x, r.y, r.width, r.height]));
    }
    // ------------------------------------------------------------ end GetWorkArea
}

// ---------------------------------------------------------------- Escape watch (#24)
// WatchEscape(true) grabs Escape as a global accelerator for the caller and emits
// EscapePressed each time it is pressed; WatchEscape(false) ungrabs it. The app calls it
// only while its menu is open, so Escape stays free for everyone else. The grab belongs
// to the caller's bus connection: when that connection closes (the app quits or crashes)
// the grab goes too, so Escape is never left grabbed.
class EscapeWatch {
    constructor(emit) {
        this._emit = emit;          // () => emit the EscapePressed signal
        this._action = Meta.KeyBindingAction.NONE;
        this._owner = null;         // unique bus name of the caller holding the grab
        this._activatedId = global.display.connect('accelerator-activated',
            (display, action) => {
                if (action !== Meta.KeyBindingAction.NONE && action === this._action)
                    this._emit();
            });
    }

    watch(on, sender) {
        if (!on) {
            if (this._owner === sender)
                this._ungrab();
            return false;
        }
        if (this._action !== Meta.KeyBindingAction.NONE)
            return this._owner === sender;  // one watcher at a time
        const action = global.display.grab_accelerator('Escape', Meta.KeyBindingFlags.NONE);
        if (action === Meta.KeyBindingAction.NONE) {
            console.warn('DragShift: could not grab Escape (another program holds it)');
            return false;
        }
        Main.wm.allowKeybinding(Meta.external_binding_name_for_action(action),
            Shell.ActionMode.ALL);
        this._action = action;
        this._owner = sender;
        return true;
    }

    // `name` left the bus (Callers.onVanished): its grab goes with it.
    forget(name) {
        if (this._owner === name)
            this._ungrab();
    }

    _ungrab() {
        if (this._action !== Meta.KeyBindingAction.NONE) {
            Main.wm.allowKeybinding(Meta.external_binding_name_for_action(this._action),
                Shell.ActionMode.NONE);
            global.display.ungrab_accelerator(this._action);
            this._action = Meta.KeyBindingAction.NONE;
        }
        this._owner = null;
    }

    destroy() {
        this._ungrab();
        global.display.disconnect(this._activatedId);
    }
}
// ---------------------------------------------------------------- end Escape watch

export default class DragShiftExtension extends Extension {
    enable() {
        if (this._dbusObject)
            return;
        const [, bytes] = this.dir.get_child('dbus.xml').load_contents(null);
        const xml = new TextDecoder().decode(bytes);
        this._callers = new Callers(isTestSession());
        this._placer = new WindowPlacer();
        this._service = new DragShiftService(this.metadata, this._callers, this._placer);
        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(xml, this._service);
        const escape = new EscapeWatch(
            () => this._dbusObject?.emit_signal('EscapePressed', null));
        this._service.escape = escape;
        const placer = this._placer;
        this._callers.onVanished(name => {
            escape.forget(name);
            placer.forget(name);
        });
        this._dbusObject.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null, null);
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._dbusObject) {
            this._dbusObject.unexport();
            this._dbusObject = null;
        }
        this._service?.escape?.destroy();
        this._service = null;
        this._placer?.destroy();
        this._placer = null;
        this._callers?.destroy();
        this._callers = null;
    }
}
