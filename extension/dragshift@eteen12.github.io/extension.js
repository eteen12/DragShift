// DragShift GNOME Shell extension: the D-Bus service the DragShift app asks for what
// Wayland withholds from ordinary clients. The interface is in dbus.xml.
//
// enable() exports the object and owns the bus name; disable() undoes both and keeps
// no state. Both are idempotent. Nothing in the shell (Main, the stage) is patched.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.eteen12.DragShift';
const OBJECT_PATH = '/io/github/eteen12/DragShift';
// CaptureArea: at most this many strips, about this many logical pixels each.
const CAPTURE_MAX_STRIPS = 4;
const CAPTURE_STRIP_PIXELS = 40000;

class DragShiftService {
    constructor(metadata) {
        this._version = metadata['version-name'] ?? 'unknown';
    }

    Version() {
        return this._version;
    }

    GetPointer() {
        const [x, y, mask] = global.get_pointer();
        return [x, y, mask];
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
    CaptureAreaAsync([x, y, width, height], invocation) {
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
            .then(() => this._captureArea(x0, y0, x1 - x0, y1 - y0))
            .then(strips => invocation.return_value(GLib.Variant.new_tuple([
                ...[x0, y0, x1 - x0, y1 - y0].map(v => new GLib.Variant('i', v)),
                GLib.Variant.new_array(ay,
                    strips.map(png => GLib.Variant.new_from_bytes(ay, png, true)))])))
            .catch(e => invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed',
                String(e?.message ?? e)));
    }

    // Promise of the PNGs (GLib.Bytes) of the rect's strips, top to bottom.
    _captureArea(x, y, width, height) {
        const n = Math.max(1, Math.min(CAPTURE_MAX_STRIPS, height,
            Math.round(width * height / CAPTURE_STRIP_PIXELS)));
        const shots = [];
        const hidden = [global.compositor.get_feedback_group(),
            ...global.get_window_actors().filter(a =>
                a.get_meta_window()?.get_window_type() === Meta.WindowType.DND)]
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
}

export default class DragShiftExtension extends Extension {
    enable() {
        if (this._dbusObject)
            return;
        const [, bytes] = this.dir.get_child('dbus.xml').load_contents(null);
        const xml = new TextDecoder().decode(bytes);
        this._service = new DragShiftService(this.metadata);
        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(xml, this._service);
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
        this._service = null;
    }
}
