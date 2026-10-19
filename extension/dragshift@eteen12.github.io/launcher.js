// The DragShift app as the extension's own Wayland client (#95). Serves Launch in dbus.xml.
//
// Mutter lists every window of an ordinary Wayland client in the dock and in Alt+Tab, and
// focuses it when it maps; a client cannot change that. For a client the compositor
// started itself (Meta.WaylandClient) the shell may make a window dock-type: no dock icon,
// no Alt+Tab entry, never focused on map. So the app asks Launch() to be started this way
// (dragshift/handoff.py), and placement.js makes its menu and toasts dock-type. Measured
// alternatives (Qt window types, transient parents, make_dock on a window the client does
// not own) are on #95; none of them work.
//
// - Only the installed launcher ~/.local/bin/dragshift is ever started, with fixed
//   arguments: nothing from the bus picks what runs. It must be a regular file (not a
//   symlink) of this user, executable and not writable by group or others. It execs
//   `python -m dragshift` (#50), so the spawned pid is the app's pid, which the #93 caller
//   check accepts.
// - At most one such process at a time. There is no restart after it exits (quit, crash or
//   the hang guard below): the next start from the app grid, the command line or the login
//   autostart (#47) asks Launch again.
// - Windows of such a client report gnome-shell's own pid (Meta.Window.get_pid()), so
//   windowPid() maps them to the spawned process for PlaceWindow and the captures.
// - Hang guard: the same pid mix-up makes GNOME's "not responding" dialog dangerous. Its
//   default button, Force Quit, runs meta_window_kill(), which SIGKILLs get_pid(), i.e.
//   gnome-shell itself, ending a Wayland session (measured on #95). So as soon as that
//   dialog appears on one of our client's windows, the guard SIGKILLs the DragShift
//   process: what Force Quit should do, before anyone can press it.
// - The process handle is module state that outlives disable(): GNOME disables extensions
//   while the screen is locked, and after unlocking the extension must still know which
//   windows are DragShift's. disable() drops every signal connection; the app keeps running.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';

// The env var that tells the started app it is the managed one (so it never asks again).
export const MANAGED_ENV = 'DRAGSHIFT_MANAGED';

// {client: Meta.WaylandClient, process: Gio.Subprocess, pid} while the started app runs.
let current = null;

export function launcherPath() {
    return GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'dragshift']);
}

// Why `path` must not be started, or null when it may.
export function launcherRefusal(path, uid) {
    let info;
    try {
        info = Gio.File.new_for_path(path).query_info(
            'standard::type,unix::uid,unix::mode,access::can-execute',
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
    } catch (e) {
        return `no launcher at ${path}`;
    }
    if (info.get_file_type() !== Gio.FileType.REGULAR)
        return `${path} is not a regular file`;
    if (info.get_attribute_uint32('unix::uid') !== uid)
        return `${path} belongs to another user`;
    if (info.get_attribute_uint32('unix::mode') & 0o022)
        return `${path} is writable by group or others`;
    if (!info.get_attribute_boolean('access::can-execute'))
        return `${path} is not executable`;
    return null;
}

// Start the app as our Wayland client unless it already runs. quiet: pass --quiet (no
// start-up toast). Returns null on success (started now or already running), else why not.
export function launch(quiet) {
    if (current)
        return null;
    const path = launcherPath();
    const why = launcherRefusal(path, new Gio.Credentials().get_unix_user());
    if (why)
        return why;
    const launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE);
    launcher.set_cwd(GLib.get_home_dir());
    launcher.setenv(MANAGED_ENV, '1', true);
    const client = Meta.WaylandClient.new(global.context, launcher);
    const process = client.spawnv(global.display, quiet ? [path, '--quiet'] : [path]);
    const pid = Number(process.get_identifier());
    const started = {client, process, pid};
    current = started;
    console.log(`DragShift: started the app (pid ${pid}) as the shell's Wayland client`);
    process.wait_async(null, (p, res) => {
        try {
            p.wait_finish(res);
        } catch {}
        const how = p.get_if_signaled() ? `killed by signal ${p.get_term_sig()}`
            : `exit status ${p.get_exit_status()}`;
        console.log(`DragShift: the app (pid ${pid}) ended, ${how}; not restarted`);
        if (current === started)
            current = null;
    });
    return null;
}

// Whether `window` belongs to the app we started.
export function isManaged(window) {
    return current !== null && current.client.owns_window(window);
}

// The pid of the process that owns `window`: get_pid(), except for our client's windows,
// which report gnome-shell's pid.
export function windowPid(window) {
    return isManaged(window) ? current.pid : window.get_pid();
}

// Make one of our client's windows dock-type (see the top of this file).
export function makeDock(window) {
    if (isManaged(window) && window.get_window_type() !== Meta.WindowType.DOCK)
        current.client.make_dock(window);
}

// While enabled: SIGKILL the started app as soon as GNOME's not-responding dialog shows
// on one of its windows (see the top of this file). The dialog is a Dialog.Dialog added
// as a child of the window's actor (js/ui/closeDialog.js).
export class HangGuard {
    constructor() {
        this._watched = new Map();    // window -> {ids (on the window), actor, actorId}
        this._createdId = global.display.connect('window-created',
            (_display, window) => this._watch(window));
        global.get_window_actors().forEach(a => this._watch(a.get_meta_window()));
    }

    _watch(window) {
        if (!window || !isManaged(window) || this._watched.has(window))
            return;
        const entry = {ids: [], actor: null, actorId: 0};
        this._watched.set(window, entry);
        // A new Wayland window has no actor yet at window-created: hook it once shown.
        const hook = () => {
            const actor = window.get_compositor_private();
            if (!actor || entry.actor)
                return;
            entry.actor = actor;
            entry.actorId = actor.connect('child-added', (_actor, child) => {
                if (child instanceof Dialog.Dialog)
                    this._kill(window, child);
            });
        };
        entry.ids.push(window.connect('shown', hook));
        entry.ids.push(window.connect('unmanaging', () => this._unwatch(window)));
        hook();
    }

    _unwatch(window) {
        const entry = this._watched.get(window);
        if (!entry)
            return;
        entry.ids.forEach(id => window.disconnect(id));
        if (entry.actorId)
            entry.actor.disconnect(entry.actorId);
        this._watched.delete(window);
    }

    _kill(window, dialog) {
        if (!isManaged(window))
            return;
        dialog.makeInactive();        // nobody can press Force Quit meanwhile
        console.warn(`DragShift: the app (pid ${current.pid}) stopped responding; ` +
            'killing it, because Force Quit would kill the shell');
        current.process.force_exit();
    }

    destroy() {
        global.display.disconnect(this._createdId);
        [...this._watched.keys()].forEach(window => this._unwatch(window));
    }
}
