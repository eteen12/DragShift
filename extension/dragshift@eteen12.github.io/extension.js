// DragShift GNOME Shell extension: the D-Bus service the DragShift app asks for what
// Wayland withholds from ordinary clients. The interface is in dbus.xml.
//
// enable() exports the object and owns the bus name; disable() undoes both and keeps
// no state. Both are idempotent. Nothing in the shell (Main, the stage) is patched.

import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.eteen12.DragShift';
const OBJECT_PATH = '/io/github/eteen12/DragShift';

class DragShiftService {
    constructor(metadata) {
        this._version = metadata['version-name'] ?? 'unknown';
    }

    Version() {
        return this._version;
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
