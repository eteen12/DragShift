# Linux development setup

Ubuntu 24.04, GNOME Shell 46 on Wayland, Python 3.12, from the repo root. How to run the
tests, the isolated GNOME session, Docker and the E2E runner is in
[LINUX-TESTING.md](LINUX-TESTING.md).

## System packages

The unit tests only need Python's `venv` module. Anything that starts Qt (`--selftest`,
the app itself) also needs the Qt runtime libraries below. These are the packages a bare
`ubuntu:24.04` image is missing; a normal Ubuntu desktop has most of them already.

```sh
# Unit tests
sudo apt-get install -y python3-venv

# Qt: offscreen platform (selftest, headless tests)
sudo apt-get install -y libgl1 libegl1 libfontconfig1 libfreetype6 libglib2.0-0t64 \
  libdbus-1-3 libx11-6 libxkbcommon0 libgssapi-krb5-2

# Qt: xcb (X11/XWayland) and wayland platforms
sudo apt-get install -y libx11-xcb1 libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-randr0 libxcb-render0 libxcb-render-util0 libxcb-shape0 \
  libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0 \
  libwayland-client0 libwayland-cursor0 libwayland-egl1
```

`libxcb-cursor0` is not part of a default Ubuntu 24.04 desktop. Without it Qt refuses to
load the `xcb` plugin. DragShift runs as a native Wayland client on GNOME
([display-backend.md](linux/display-backend.md)), so only the xcb comparison tests need it.

The isolated GNOME session also needs `gnome-shell`, `dbus`, `gdbus` and `python3-gi`,
which a normal Ubuntu desktop has.

## Environment

```sh
uv venv .venv
uv pip install -r requirements.txt pytest
uv run pytest -m "not gnome"
```

Without uv:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q -m "not gnome"
```

Run the app only inside the isolated session (LINUX-TESTING.md), never on your desktop
while testing.

## Self-test and log file

```sh
XDG_STATE_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen .venv/bin/python -m dragshift --selftest
```

It prints `SELFTEST OK` and exits 0. On Linux the app logs to `$XDG_STATE_HOME/DragShift/log.txt`
(default `~/.local/state/DragShift/log.txt`); a log over 1 MB is moved to `log.old.txt` at
start. The temporary `XDG_STATE_HOME` keeps the self-test's log out of your real one.

## How the Linux parts work

### Start-up

`app.main()` calls `linux_qpa()` before `QApplication` exists and `linux_setup(qapp)` right
after (`dragshift/app.py`, #11):

- **Qt platform.** A `QT_QPA_PLATFORM` or `-platform` you set always wins. Otherwise
  `DRAGSHIFT_QPA` picks one (for tests), a Wayland session gets `wayland;xcb`, and an X11
  session keeps Qt's default. It is passed as a `-platform` argument, so programs DragShift
  starts (`xdg-open`, LibreOffice) don't inherit it.
- **Fonts.** Segoe UI is substituted with `Ubuntu Sans`, `Ubuntu`, `Cantarell`, `Noto Sans`
  or `DejaVu Sans` (the first installed one).
- **App id.** `setDesktopFileName("io.github.eteen12.DragShift")`, so GNOME matches every
  window to `io.github.eteen12.DragShift.desktop`.
- **HiDPI.** The `PassThrough` rounding policy uses each monitor's exact scale.
- The log gets `platform: wayland`, one `screen …` line per monitor and the resolved font.

### GNOME Shell extension

DragShift's own extension, `extension/dragshift@eteen12.github.io/`, owns
`io.github.eteen12.DragShift` on the session bus (interface in `dbus.xml`).
`dragshift/platform_linux.py` is the client (`jeepney`, 200 ms timeout, `None` when the
extension doesn't answer). It gives the app what Wayland withholds:

- `GetPointer`: the global pointer with the Shift/Ctrl/Alt and button state.
- `CaptureArea`, `CaptureAreaExcludingCaller`: the screen under a rect, with the drag
  icon (and optionally the caller's own windows) hidden.
- `WatchEscape` / `EscapePressed`: a global Escape grab.
- `PlaceWindow`: places, raises and un-focuses the caller's own windows.
- `GetWorkArea(x, y)`, `GetOwnWindowFrame(title)`: the monitor's work area, and where
  Mutter put one of the caller's windows.

`extension/build.sh` writes `dist/dragshift@eteen12.github.io.shell-extension.zip` with the
app version in `metadata.json`; `Version()` returns it, and the app warns when the two
differ. GNOME on Wayland loads a newly installed extension only after you log out and back
in, so try changes in the isolated session.

#### Who may call the extension (trust model)

Every method but `Version` hands out what GNOME withholds from ordinary apps, so it
answers only the DragShift app (`caller.js`, #93). `Version` stays open so the app and the
installer can find the extension.
- The caller's uid (from the bus daemon) must be the shell's. `/proc/<pid>/exe` must be a
  Python interpreter, and its command line `python -m dragshift`,
  `python …/dragshift/__main__.py` or the launcher `~/.local/bin/dragshift`.
  `python run.py` is refused.
- Anyone else gets `org.freedesktop.DBus.Error.AccessDenied`, and the journal gets one
  `DragShift: refused <method> to <bus name> (pid N): <reason>` line per connection.
- The verdict is cached per bus connection and dropped when it leaves the bus (about
  0.9 ms for the first call; `GetPointer` then takes 0.20 ms).
- In a launcher session only, pytest, `python -m tests.linux.<module>` and scripts in a
  `tests/linux` directory are let in too (LINUX-TESTING.md).

It keeps out casual and accidental callers: `gdbus`, scripts and other programs of yours
that would screenshot the screen or follow the pointer quietly. It is defence in depth,
not a boundary against code that already runs as you: such code can start Python with
DragShift's command line or attach a debugger to the app, and could read your files
anyway (`tests/linux/test_caller_check.py` shows this on purpose). Other users can't reach
your session bus, and Flatpak and snap apps don't see the name through their D-Bus
proxies. A process that `exec`s something else keeps its connection and its verdict.

### Trigger, menu and Escape

- The watcher polls `pointer_state()` every 15 ms and opens the menu on a Shift/Ctrl/Alt drag (#31).
  Qt never sees Shift during a drag, so ring flips also come from the watcher.
- During a file drag, Mutter's grab takes Escape and cancels the drag; the menu gets a
  `dragLeave` about 1–2 ms later with the button still down. The menu closes when that
  has lasted `trigger.DRAG_CANCEL_GRACE` (150 ms) over the menu (#32).
- Drops (#35): paths are read from `text/uri-list` when the drag enters; non-local URIs
  are ignored. The menu always answers `CopyAction`, so the source file never moves. A
  drop beside the petals is refused and closes the menu.

### Click mode

The tray's *Open a file…*, Open With and a second `dragshift <file>` open the menu without
a drag (#36). It opens at the pointer from the extension, or at the centre of the primary
screen without it. It never has the keyboard focus, so it holds `watch_escape(True)` while
open. `grabMouse()` only works between DragShift's own windows on Wayland, so a thread
reads `pointer_state()` every `radial.CLICK_POLL_MS` (8 ms): a button going down outside
the menu closes it, Shift flips the ring. The click also reaches what is under it.

### Capture and glass

- `platform_linux.capture(x, y, w, h)` returns a PIL image of exactly w×h, or None. The
  extension paints the rect offscreen with the drag icon hidden and returns up to four
  PNG strips encoded in parallel (400×400 in about 11–17 ms). The cursor is never in it.
- `capture(..., exclude_self=True)` also hides every window of the calling process (by
  pid from the bus daemon), for backdrops behind a visible toast or window.
- `glass.grab_screen` is the one capture path: extension capture, then the wallpaper
  crop, then Qt's grab only when `below_window` is 0.
- Wayland never tells a client where its windows are. Toasts are placed by the app
  (`PlaceWindow`), so `ToastCard.stage_rect()` is known. Glass windows poll
  `GetOwnWindowFrame` every 100 ms while shown and refresh their backdrop 140 ms after a
  move settles, on a pool thread over a second jeepney connection (#40, #107).
- Toasts stack inside `GetWorkArea`, because Qt's `availableGeometry()` is the whole
  monitor under Wayland (#42).

### Tray

`QSystemTrayIcon` is published as a StatusNotifierItem with a `com.canonical.dbusmenu`
menu, shown by Ubuntu's AppIndicator extension (#46).
- It sends the 16–64 px frames of `assets/dragshift.ico` as pixmaps, no icon name.
- A click opens the menu, a double click opens the file picker, a middle click is ignored.
- *Tip the developer ♥* runs `xdg-open` (Qt's `openUrl` would ask "Open with…").
- Without a StatusNotifier host the app keeps running, logs `tray: no StatusNotifierWatcher`,
  and opening DragShift again shows Settings instead of the "already running" toast.

### Open With and the command line

`dragshift a.jpg b.jpg` hands the files to the running DragShift (one click-mode menu for
all of them) or starts it (#50).
- `dragshift/handoff.py` sends `{"files": [...]}` to the app's `QLocalServer` before Qt is
  imported (about 20–35 ms).
- The socket is `$XDG_RUNTIME_DIR/DragShift`, falling back to `$TMPDIR/DragShift-$USER`
  without a private runtime dir. Windows and macOS are unchanged.
- Relative paths are resolved against the caller's directory, missing paths are dropped,
  and launches within 280 ms are merged into one menu.

`packaging/linux/` holds what `install-linux.sh` (#66) installs:
- `io.github.eteen12.DragShift.desktop`: `Exec=dragshift %F` and a `MimeType` for every
  supported type, so DragShift appears under *Open With*. Check it with
  `desktop-file-validate`.
- `dragshift`: the `~/.local/bin/dragshift` launcher. It ends in
  `exec "$VENV/bin/python" -m dragshift "$@"`, the form the extension's caller check
  accepts. Keep it that way.
- `nautilus-scripts/DragShift…`: Files' right-click → Scripts → DragShift… (#51). It runs
  the launcher with the selected files (from its arguments, then
  `NAUTILUS_SCRIPT_SELECTED_URIS`), falling back to `dragshift` on `PATH` or
  `python -m dragshift` in `DRAGSHIFT_SRC`.

### DOCX → PDF and LibreOffice

DOCX → PDF uses LibreOffice when installed (`soffice` or `libreoffice` on `PATH`, the snap
at `/snap/bin/libreoffice`, or the flatpak `org.libreoffice.LibreOffice`) and falls back to
the built-in renderer otherwise (#53).
- Each conversion runs headless with a throwaway profile and a copy of the source, in
  `~/snap/<snap>/common/DragShift/` for the snap (it can't use `/tmp` or hidden folders)
  and `$XDG_CACHE_HOME/DragShift/soffice/` otherwise. Your own profile is never used.
- LibreOffice runs in its own process group, killed afterwards. The snap ignores signals
  from confined callers, so `platform_linux.kill_process_group` then stops its
  `snap.*.scope` with `systemctl --user`.
