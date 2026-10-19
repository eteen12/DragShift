"""A D-Bus caller of DragShift's shell extension for the caller-check tests (#93).

The extension answers its protected methods only to the DragShift app, which it tells by
the caller's command line. The tests run this same file under different command lines
(`python -m tests.linux.caller`, a plain `python /tmp/.../probe.py`, a copy saved as
`<tmp>/dragshift/__main__.py` and run as `python -m dragshift`), so it imports nothing
but the standard library and jeepney.

    python -m tests.linux.caller             # print {method: "ok" | D-Bus error name}
    python -m tests.linux.caller hold-escape # WatchEscape(true), print the reply, keep it

The bus is DBUS_SESSION_BUS_ADDRESS (the isolated session's when run through it).
"""
import json
import sys
import time

BUS_NAME = "io.github.eteen12.DragShift"
OBJECT_PATH = "/io/github/eteen12/DragShift"
INTERFACE = "io.github.eteen12.DragShift"

# Every method with harmless arguments: a 10x10 capture, Escape released (never grabbed),
# a placement rule for a title no window has (dropped when this connection closes).
PROTECTED = {
    "GetPointer": (None, ()),
    "CaptureArea": ("iiii", (0, 0, 10, 10)),
    "CaptureAreaExcludingCaller": ("iiii", (0, 0, 10, 10)),
    "WatchEscape": ("b", (False,)),
    "PlaceWindow": ("sii", ("no such window", 0, 0)),
}
OPEN = {"Version": (None, ())}


def connect():
    from jeepney.io.blocking import open_dbus_connection
    return open_dbus_connection(bus="SESSION")


def call(conn, method: str, signature=None, args=(), timeout: float = 5):
    """("ok", reply body) or (D-Bus error name, message)."""
    from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
    msg = new_method_call(DBusAddress(OBJECT_PATH, BUS_NAME, INTERFACE), method,
                          signature, args)
    reply = conn.send_and_get_reply(msg, timeout=timeout)
    if reply.header.message_type == MessageType.method_return:
        return "ok", reply.body
    return reply.header.fields.get(HeaderFields.error_name), reply.body


def call_all(conn) -> dict:
    """{method: "ok" or the D-Bus error name} for Version and every protected method."""
    return {m: call(conn, m, sig, args)[0] for m, (sig, args) in {**OPEN, **PROTECTED}.items()}


def main(argv) -> int:
    with connect() as conn:
        if argv[1:2] == ["hold-escape"]:
            print(json.dumps(call(conn, "WatchEscape", "b", (True,))), flush=True)
            while True:                     # the grab lasts as long as this connection
                time.sleep(60)
        print(json.dumps(call_all(conn)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
