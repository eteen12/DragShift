// Who may call DragShift's D-Bus methods (#93). The one place the extension asks who its
// caller is.
//
// Every method but Version hands out something GNOME keeps from ordinary clients: screen
// pixels (CaptureArea*), the global pointer and Shift/Ctrl/Alt state (GetPointer), a
// global Escape grab (WatchEscape), window placement (PlaceWindow) and the monitors' work
// areas (GetWorkArea). They answer only the DragShift app, which is
// - a process of the same user (UnixUserID from the bus daemon), whose
// - executable is a Python interpreter (/proc/<pid>/exe), and whose
// - command line (/proc/<pid>/cmdline) runs the app: `python -m dragshift`,
//   `python .../dragshift/__main__.py`, or the installed launcher ~/.local/bin/dragshift
//   (#66; it execs `python -m dragshift`, and as a Python script it would be the
//   script argument).
// Everyone else gets org.freedesktop.DBus.Error.AccessDenied.
//
// Trust model: this is defence in depth, not a sandbox. Any process of the same user can
// start a Python with that command line (or ptrace or inject into the real app), so it
// keeps out casual callers (gdbus, scripts, other apps of the user), not determined
// malware. Other users never reach this bus. Flatpak and snap apps don't see this name
// through their D-Bus proxies at all. docs/LINUX-DEV.md says the same.
//
// The verdict is asked once per unique bus name (pid and uid in one
// GetConnectionCredentials call, then the two /proc reads) and kept until that name leaves
// the bus (NameOwnerChanged), so GetPointer's 15 ms poll costs one Map lookup.
//
// Test sessions only: in a headless shell started by the test launcher (session root
// <tmp>/dragshift-session-*, tests/linux/session.py; the same guard as the test helper
// extension's input injection) the test processes are let in too: pytest, a Python
// running `-m tests.linux.<module>` or a script in a tests/linux directory. Nowhere else.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

export const ACCESS_DENIED = 'org.freedesktop.DBus.Error.AccessDenied';
const SESSION_ROOT_PREFIX = 'dragshift-session-';
const PYTHON = /^python[0-9.]*$/;

// What a Python command line runs, parsed like CPython does: {module}, {script}, or null
// when argv is not a Python running a module or script file (-c, stdin, interactive).
export function pythonTarget(argv) {
    if (!PYTHON.test(GLib.path_get_basename(argv[0] ?? '')))
        return null;
    for (let i = 1; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === '--')
            return argv[i + 1] ? {script: argv[i + 1]} : null;
        if (arg === '-')
            return null;
        if (arg.startsWith('--')) {      // --check-hash-based-pycs MODE; others take none
            if (arg === '--check-hash-based-pycs')
                i++;
            continue;
        }
        if (!arg.startsWith('-'))
            return {script: arg};
        // Bundled short options (-IB); -c, -m, -X and -W end the bundle.
        for (let j = 1; j < arg.length; j++) {
            const c = arg[j];
            if (c === 'c')
                return null;
            if (c === 'm' || c === 'X' || c === 'W') {
                const value = arg.slice(j + 1) || argv[++i];
                if (c === 'm')
                    return value ? {module: value} : null;
                break;
            }
        }
    }
    return null;
}

// Whether argv (from /proc/<pid>/cmdline) runs the DragShift app; `home` is $HOME.
export function isDragShiftCommand(argv, home) {
    const target = pythonTarget(argv);
    if (!target)
        return false;
    if (target.module !== undefined)
        return target.module === 'dragshift';
    const script = target.script;
    return script === 'dragshift/__main__.py' || script.endsWith('/dragshift/__main__.py') ||
        script === `${home}/.local/bin/dragshift`;
}

// Whether argv runs the test suite or one of its helpers (only honoured in a test session).
export function isTestCommand(argv) {
    const target = pythonTarget(argv);
    if (!target)
        return false;
    if (target.module !== undefined)
        return target.module === 'pytest' || /^tests\.linux\.\w+$/.test(target.module);
    const name = GLib.path_get_basename(target.script);
    return name === 'pytest' || name === 'py.test' ||
        /(^|\/)tests\/linux\/\w+\.py$/.test(target.script);
}

// True in a headless shell started by the test launcher (see the top of this file).
export function isTestSession() {
    const root = GLib.path_get_dirname(GLib.get_user_data_dir());
    return global.backend.is_headless() &&
        GLib.path_get_basename(root).startsWith(SESSION_ROOT_PREFIX);
}

// The verdict for a process of this user: null when it may call, else why not.
export function refusal(pid, home, testSession) {
    let exe, argv;
    try {
        exe = GLib.file_read_link(`/proc/${pid}/exe`).replace(/ \(deleted\)$/, '');
        const [, bytes] = GLib.file_get_contents(`/proc/${pid}/cmdline`);
        argv = new TextDecoder().decode(bytes).split('\0');
        if (argv.at(-1) === '')
            argv.pop();
    } catch (e) {
        return `cannot read /proc/${pid}: ${e.message}`;
    }
    if (!PYTHON.test(GLib.path_get_basename(exe)))
        return `${exe} is not a Python interpreter`;
    if (isDragShiftCommand(argv, home) || (testSession && isTestCommand(argv)))
        return null;
    return 'not the DragShift app';
}

export class Callers {
    // testSession: let the test processes in too (isTestSession()).
    constructor(testSession = false) {
        this._bus = Gio.DBus.session;
        this._uid = new Gio.Credentials().get_unix_user();
        this._home = GLib.get_home_dir();
        this._testSession = testSession;
        this._entries = new Map();   // unique name -> {verdict: {pid, why} | null, promise}
        this._vanished = new Set();  // callbacks(name) when a checked name leaves the bus
        this._subscription = this._bus.signal_subscribe('org.freedesktop.DBus',
            'org.freedesktop.DBus', 'NameOwnerChanged', '/org/freedesktop/DBus', null,
            Gio.DBusSignalFlags.NONE, (_conn, _from, _path, _iface, _signal, params) => {
                const [name, , owner] = params.deepUnpack();
                if (owner === '' && this._entries.delete(name))
                    this._vanished.forEach(cb => cb(name));
            });
    }

    destroy() {
        this._bus.signal_unsubscribe(this._subscription);
        this._entries.clear();
        this._vanished.clear();
    }

    // Call cb(uniqueName) when a name that called a method leaves the bus (its process
    // quit or closed the connection).
    onVanished(cb) {
        this._vanished.add(cb);
    }

    // Run serve(pid) for `invocation` when its sender is the DragShift app, else reply
    // AccessDenied. Synchronous once the sender's verdict is known.
    serve(invocation, serve) {
        const sender = invocation.get_sender();
        if (!sender) {
            this._deny(invocation, 'no sender');
            return;
        }
        let entry = this._entries.get(sender);
        if (!entry) {
            entry = {verdict: null, promise: null};
            entry.promise = this._check(sender, invocation.get_method_name()).then(
                verdict => (entry.verdict = verdict),
                e => {
                    // The bus could not tell (e.g. the sender already left): ask again
                    // next time instead of caching a guess.
                    if (this._entries.get(sender) === entry)
                        this._entries.delete(sender);
                    return {pid: 0, why: `${e.message ?? e}`};
                });
            this._entries.set(sender, entry);
        }
        if (entry.verdict) {
            this._answer(invocation, entry.verdict, serve);
            return;
        }
        entry.promise.then(verdict => {
            if (this._entries.get(sender) !== entry && !verdict.why)
                verdict = {pid: 0, why: 'the caller left the bus'};
            this._answer(invocation, verdict, serve);
        });
    }

    _answer(invocation, verdict, serve) {
        if (verdict.why) {
            this._deny(invocation, verdict.why);
            return;
        }
        try {
            serve(verdict.pid);
        } catch (e) {
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', `${e}`);
        }
    }

    _deny(invocation, why) {
        invocation.return_dbus_error(ACCESS_DENIED,
            `${invocation.get_method_name()} only answers the DragShift app (${why})`);
    }

    // Promise of {pid, why}: why is null for the DragShift app, else the reason.
    async _check(sender, method) {
        const reply = await new Promise((resolve, reject) => {
            this._bus.call('org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'GetConnectionCredentials',
                new GLib.Variant('(s)', [sender]), new GLib.VariantType('(a{sv})'),
                Gio.DBusCallFlags.NONE, 1000, null, (conn, res) => {
                    try {
                        resolve(conn.call_finish(res));
                    } catch (e) {
                        reject(e);
                    }
                });
        });
        const [creds] = reply.recursiveUnpack();
        const pid = creds.ProcessID ?? 0, uid = creds.UnixUserID;
        let why;
        if (!(pid > 0) || uid === undefined)
            why = 'the bus does not know its process';
        else if (uid !== this._uid)
            why = `user ${uid}`;
        else
            why = refusal(pid, this._home, this._testSession);
        if (why)
            console.warn(`DragShift: refused ${method} to ${sender} (pid ${pid}): ${why}`);
        return {pid, why};
    }
}
