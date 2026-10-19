// DragShift GNOME Shell extension: the D-Bus service the DragShift app asks for what
// Wayland withholds from ordinary clients. The interface is in dbus.xml.
//
// enable() exports the object and owns the bus name; disable() undoes both and drops the
// window-placement rules (placement.js). Both are idempotent. Nothing in the shell (Main,
// the stage) is patched.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {WindowPlacer} from './placement.js';

const BUS_NAME = 'io.github.eteen12.DragShift';
const OBJECT_PATH = '/io/github/eteen12/DragShift';

class DragShiftService {
    constructor(metadata, placer) {
        this._version = metadata['version-name'] ?? 'unknown';
        this._placer = placer;
    }

    Version() {
        return this._version;
    }

    GetPointer() {
        const [x, y, mask] = global.get_pointer();
        return [x, y, mask];
    }

    // Window placement (#10), see placement.js.
    PlaceWindowAsync([title, x, y], invocation) {
        this._placer.place(invocation.get_sender(), title, x, y,
            placed => invocation.return_value(new GLib.Variant('(b)', [placed])),
            e => invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', `${e}`));
    }
}

export default class DragShiftExtension extends Extension {
    enable() {
        if (this._dbusObject)
            return;
        const [, bytes] = this.dir.get_child('dbus.xml').load_contents(null);
        const xml = new TextDecoder().decode(bytes);
        this._placer = new WindowPlacer();
        this._service = new DragShiftService(this.metadata, this._placer);
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
        this._placer?.destroy();
        this._placer = null;
    }
}
