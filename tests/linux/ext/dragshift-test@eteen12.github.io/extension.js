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

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'io.github.eteen12.DragShiftTest';
const OBJECT_PATH = '/io/github/eteen12/DragShiftTest';
const SESSION_ROOT_PREFIX = 'dragshift-session-';

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
    <method name="ActivateWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="found"/>
    </method>
    <method name="GetPointer">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="u" direction="out" name="mask"/>
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

function describe(actor, stacking, focus) {
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
    };
}

class Helper {
    constructor(version) {
        this._version = version;
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
        return global.get_window_actors().map((actor, i) => describe(actor, i, focus));
    }

    // id is Meta.Window.get_id() (see ListWindows "id") or an X11 window id.
    // Main.activateWindow is the shell's own path (dash, Alt+Tab): Meta.Window.activate()
    // plus switching workspace and leaving the overview if needed.
    ActivateWindow(id) {
        const windows = global.get_window_actors().map(a => a.get_meta_window());
        const w = windows.find(win => win.get_id() === id) ??
            windows.find(win => id !== 0 && xid(win) === id);
        if (!w)
            return false;
        Main.activateWindow(w, global.get_current_time());
        return true;
    }

    GetPointer() {
        const [x, y, mask] = global.get_pointer();
        return [x, y, mask];
    }
}

export default class DragShiftTestExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(
            IFACE, new Helper(String(this.metadata.version)));
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
            this._ownName();
        });
        Main.overview.hide();
    }

    _ownName() {
        this._ownerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
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
    }
}
