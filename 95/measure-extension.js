// THROWAWAY measurement extension for #95; only ever loaded in the isolated test session.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `<node><interface name="io.github.eteen12.Measure95">
<method name="Snapshot"><arg type="s" direction="out"/></method>
<method name="Spawn"><arg type="as" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
<method name="Foreign"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Kill"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
<method name="Events"><arg type="s" direction="out"/></method>
</interface></node>`;
const TYPES = Object.fromEntries(Object.entries(Meta.WindowType).map(([k, v]) => [v, k]));

class M {
    constructor() {
        this.clients = [];   // {client, proc, mode}
        this.events = [];
        this._created = global.display.connect('window-created', (_d, w) => this._onCreated(w));
        this._appState = Shell.AppSystem.get_default().connect('app-state-changed', (_s, app) =>
            this.events.push({t: GLib.get_monotonic_time(), ev: 'app-state', app: app.get_id(), state: app.state}));
    }
    destroy() {
        global.display.disconnect(this._created);
        Shell.AppSystem.get_default().disconnect(this._appState);
    }
    _owner(w) {
        return this.clients.find(c => c.client.owns_window(w));
    }
    _onCreated(w) {
        const c = this._owner(w);
        this.events.push({t: GLib.get_monotonic_time(), ev: 'created', title: w.get_title(), owned: !!c,
            type: TYPES[w.get_window_type()]});
        if (!c)
            return;
        const apply = when => {
            if (c.mode === 'dock-created' || (c.mode === 'dock-title' && w.get_title()?.startsWith('probe'))) {
                if (w.get_window_type() !== Meta.WindowType.DOCK) {
                    c.client.make_dock(w);
                    this.events.push({t: GLib.get_monotonic_time(), ev: `make_dock@${when}`, title: w.get_title()});
                }
            } else if (c.mode === 'hide-created') {
                c.client.hide_from_window_list(w);
                this.events.push({t: GLib.get_monotonic_time(), ev: `hide@${when}`, title: w.get_title()});
            }
        };
        if (c.mode === 'dock-title') {
            const id = w.connect('notify::title', () => apply('title'));
            w.connect('unmanaged', () => w.disconnect(id));
        }
        if (c.mode === 'dock-shown') {
            const id = w.connect('shown', () => { w.disconnect(id); c.client.make_dock(w);
                this.events.push({t: GLib.get_monotonic_time(), ev: 'make_dock@shown', title: w.get_title()}); });
        }
        apply('created');
        w.connect('shown', () => this.events.push({t: GLib.get_monotonic_time(), ev: 'shown', title: w.get_title(),
            type: TYPES[w.get_window_type()], focused: global.display.focus_window === w}));
    }
    Snapshot() {
        const focus = global.display.focus_window;
        const tab = global.display.get_tab_list(Meta.TabList.NORMAL, null);
        const tracker = Shell.WindowTracker.get_default();
        const windows = global.get_window_actors().map(a => a.get_meta_window()).map(w => ({
            title: w.get_title(), pid: w.get_pid(), type: TYPES[w.get_window_type()],
            skip_taskbar: w.is_skip_taskbar(), above: w.is_above(), focused: w === focus,
            in_tab_list: tab.includes(w), app: tracker.get_window_app(w)?.get_id() ?? '',
            owned: !!this._owner(w), transient_for: w.get_transient_for()?.get_title() ?? null,
            frame: (r => [r.x, r.y, r.width, r.height])(w.get_frame_rect())}));
        return JSON.stringify({shell_pid: Number(GLib.getenv('MEASURE_SHELL_PID') ?? 0) || null,
            running: Shell.AppSystem.get_default().get_running().map(a => a.get_id()),
            tab: tab.map(w => w.get_title()), focus: focus?.get_title() ?? null, windows});
    }
    Spawn(argv, mode) {
        const launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE);
        const client = Meta.WaylandClient.new(global.context, launcher);
        const proc = client.spawnv(global.display, argv);
        this.clients.push({client, proc, mode});
        return true;
    }
    // Try make_dock / hide_from_window_list on a window this extension's client does NOT own.
    Foreign(title, what) {
        if (!this._dummy)
            this.Spawn(['sleep', '600'], 'none'), this._dummy = this.clients.at(-1);
        const w = global.get_window_actors().map(a => a.get_meta_window()).find(x => x.get_title() === title);
        if (!w)
            return 'no window';
        const before = {type: TYPES[w.get_window_type()], skip: w.is_skip_taskbar()};
        let err = null;
        try {
            if (what === 'dock') this._dummy.client.make_dock(w);
            else this._dummy.client.hide_from_window_list(w);
        } catch (e) { err = String(e); }
        return JSON.stringify({owns: this._dummy.client.owns_window(w), before,
            after: {type: TYPES[w.get_window_type()], skip: w.is_skip_taskbar()}, err});
    }
    Kill(title) {
        const w = global.get_window_actors().map(a => a.get_meta_window()).find(x => x.get_title() === title);
        if (!w) return false;
        w.kill();
        return true;
    }
    Events() {
        const e = JSON.stringify(this.events);
        this.events = [];
        return e;
    }
}

export default class Measure extends Extension {
    enable() {
        this._m = new M();
        this._o = Gio.DBusExportedObject.wrapJSObject(IFACE, this._m);
        this._o.export(Gio.DBus.session, '/io/github/eteen12/Measure95');
        this._n = Gio.bus_own_name(Gio.BusType.SESSION, 'io.github.eteen12.Measure95', 0, null, null, null);
    }
    disable() {
        Gio.bus_unown_name(this._n);
        this._o.unexport();
        this._m.destroy();
    }
}
