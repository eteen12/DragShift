"""Stand-in `org.freedesktop.systemd1` for the isolated session's private D-Bus.

GNOME Shell runs inside a systemd user unit here, so when Xwayland starts it asks
systemd on the *session bus* to start `gnome-session-x11-services-ready.target` and
fails the X11 display ("Failed to initialize X11 display") if that call errors. The
private bus has no systemd, and the real user manager must never be touched. This
service answers StartUnit/StopUnit with an immediately finished job and GetUnit with
NoSuchUnit, which is all the shell needs.

Started by D-Bus activation on the private bus only (session.py writes the .service file
into the session's XDG_DATA_HOME). Runs under the system python3 (needs python3-gi) and
exits when the bus goes away.
"""
import itertools
import sys

from gi.repository import Gio, GLib

PATH = "/org/freedesktop/systemd1"
IFACE = "org.freedesktop.systemd1.Manager"
XML = f"""<node><interface name="{IFACE}">
  <method name="StartUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/>
    <arg type="o" direction="out"/></method>
  <method name="StopUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/>
    <arg type="o" direction="out"/></method>
  <method name="GetUnit"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
  <signal name="JobRemoved"><arg type="u"/><arg type="o"/><arg type="s"/><arg type="s"/></signal>
</interface></node>"""

_jobs = itertools.count(1)


def _on_call(conn, _sender, _path, _iface, method, params, invocation):
    unit = params.unpack()[0]
    if method == "GetUnit":
        invocation.return_dbus_error("org.freedesktop.systemd1.NoSuchUnit",
                                     f"Unit {unit} not loaded.")
        return
    n = next(_jobs)
    job = f"{PATH}/job/{n}"
    print(f"fake-systemd: {method} {unit} -> done", file=sys.stderr, flush=True)
    invocation.return_value(GLib.Variant("(o)", (job,)))
    conn.emit_signal(None, PATH, IFACE, "JobRemoved",
                     GLib.Variant("(uoss)", (n, job, unit, "done")))


def main() -> None:
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
    conn.register_object(PATH, info, _on_call, None, None)
    loop = GLib.MainLoop()
    Gio.bus_own_name_on_connection(conn, "org.freedesktop.systemd1",
                                   Gio.BusNameOwnerFlags.NONE, None, lambda *_: loop.quit())
    loop.run()


if __name__ == "__main__":
    main()
