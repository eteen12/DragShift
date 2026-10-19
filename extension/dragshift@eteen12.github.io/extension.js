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
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'io.github.eteen12.DragShift';
const OBJECT_PATH = '/io/github/eteen12/DragShift';

class DragShiftService {
    constructor(metadata) {
        this._version = metadata['version-name'] ?? 'unknown';
        this.escape = null;     // EscapeWatch, set by enable()
    }

    Version() {
        return this._version;
    }

    GetPointer() {
        const [x, y, mask] = global.get_pointer();
        return [x, y, mask];
    }

    // See EscapeWatch below.
    WatchEscapeAsync([on], invocation) {
        invocation.return_value(new GLib.Variant('(b)',
            [this.escape.watch(on, invocation.get_sender())]));
    }
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
        this._ownerWatch = 0;
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
        this._ownerWatch = Gio.bus_watch_name(Gio.BusType.SESSION, sender,
            Gio.BusNameWatcherFlags.NONE, null, () => {
                if (this._owner === sender)
                    this._ungrab();
            });
        return true;
    }

    _ungrab() {
        if (this._ownerWatch) {
            Gio.bus_unwatch_name(this._ownerWatch);
            this._ownerWatch = 0;
        }
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
        this._service = new DragShiftService(this.metadata);
        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(xml, this._service);
        this._service.escape = new EscapeWatch(
            () => this._dbusObject?.emit_signal('EscapePressed', null));
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
    }
}
