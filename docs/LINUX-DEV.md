# Linux development setup

Ubuntu 24.04, Python 3.12, from the repo root.

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

`libxcb-cursor0` is not part of a default Ubuntu 24.04 desktop install. Without it Qt
refuses to load the `xcb` platform plugin. DragShift itself runs as a native Wayland client on GNOME
(`docs/linux/display-backend.md`), so only the xcb comparison tests need it.

## Environment and tests

```sh
uv venv .venv
uv pip install -r requirements.txt pytest
uv run pytest -q
```

Without uv:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

`tests/conftest.py` sets `QT_QPA_PLATFORM=offscreen` (unless you set it yourself), so no
`DISPLAY` or `WAYLAND_DISPLAY` is needed. It also points `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`XDG_DATA_HOME` and `XDG_CACHE_HOME` at a temporary folder for the run, so tests never touch
`~/.config/DragShift` or `~/.local`.

## Self-test and log file

```sh
XDG_STATE_HOME=$(mktemp -d) QT_QPA_PLATFORM=offscreen .venv/bin/python -m dragshift --selftest
```

It prints `SELFTEST OK` and exits 0. On Linux the app logs to `$XDG_STATE_HOME/DragShift/log.txt`
(default `~/.local/state/DragShift/log.txt`); a log over 1 MB is moved to `log.old.txt` at start.
The temporary `XDG_STATE_HOME` above keeps the self-test's log out of your real one.

## Isolated GNOME Shell session (`-m gnome` tests)

Tests marked `gnome` run inside a throwaway headless GNOME Shell 46 session started by
`tests/linux/session.py`. It needs `gnome-shell`, `dbus` (`dbus-run-session`,
`dbus-daemon`, `dbus-update-activation-environment`), `gdbus` and `python3-gi`, which a
normal Ubuntu desktop already has. Without them, the `gnome` tests are skipped.

```sh
uv run pytest -m "not gnome"                # quick loop: unit tests only, no session
uv run pytest -m gnome tests/linux          # session tests only
uv run python -m tests.linux.session -- nautilus --new-window /tmp   # run a command inside
uv run python -m tests.linux.session        # print the session env, Ctrl-C to stop
```

`--scale 2` (with `--size 3200x1800`, i.e. 1600x900 logical) scales the virtual monitor
like the user's 200 % screen, with fractional scaling on (`scale-monitor-framebuffer`);
`--size 2400x1350 --scale 1.5` gives a fractional scale. In Python: `Session(size=...,
scale=2)` or `session.set_scale(2)`. `Session(data_files={"applications/x.desktop": text})`
writes files into the session's `XDG_DATA_HOME` before the shell boots (a desktop file
that appears later is only picked up seconds afterwards).

```sh
uv run python -m tests.linux.session --size 3200x1800 --scale 2 \
  --extension /tmp/ext/dragshift@eteen12.github.io -- python -m dragshift
```

In tests, use the session-scoped `gnome_session` fixture. `gnome_session.env()` is the
environment for child processes, `gnome_session.run(cmd)` and `gnome_session.popen(cmd)`
run inside the session, and `gnome_session.log_path` is the shell's log.
`Session(extensions=[dir])` installs and enables extra extensions.

`start()` returns once the shell has finished starting up: it answers `ShellVersion`
and has logged `GNOME Shell started at …` (its `startup-complete`). Then it waits for
the helper (`helper=True`) and only then applies `scale`. GNOME Shell answers on D-Bus
before its startup animation, and a monitor change in that gap wedges its startup for good,
so the overview stays up and the helper never gets ready (#106). Change the monitors only
after `start()`. A start that stalls fails within the timeout (60 s), and the error
carries the shell log tail.

The session never touches your real desktop:
- It uses its own HOME, XDG directories, `TMPDIR`, `XDG_RUNTIME_DIR` (`/tmp/dsr.*`),
  session bus and an empty private system bus.
- It runs a private copy of DING, because DING kills every other DING of the same user
  when it starts.
- Teardown kills every process that carries the session's runtime dir, then deletes the
  temporary directories. This also happens on Ctrl-C. A session whose launcher was
  SIGKILLed is cleaned up by the next start.

### Test-helper extension (screenshots, window list, input)

GNOME 46 refuses `org.gnome.Shell.Screenshot` to callers that are not allowlisted, so
tests use a test-only extension, `dragshift-test@eteen12.github.io`
(`tests/linux/ext/`). It owns `io.github.eteen12.DragShiftTest` on the session's private
bus and offers `Version`, `Screenshot(path)`, `ScreenshotArea(x, y, w, h, path)`,
`ListWindows`, `ActivateWindow(id)`, `MoveWindow(id, x, y, w, h)`, `GetPointer`,
`GetWorkArea(x, y)` (monitor and Mutter's work area there) and the input methods below.

- `Session(helper=True)` (CLI: `--helper`) copies it into the session's temporary
  `XDG_DATA_HOME` and enables it. The shared `gnome_session` fixture always does this.
  Nothing installs it anywhere else, and it must never go on a real desktop.
- `tests/linux/shell.py` is the client. The `shell_helper` fixture gives a ready
  `ShellHelper`: `screenshot()`, `screenshot_area(...)`, `list_windows()`,
  `wait_for_window(wm_class=...)`, `activate_window(id)`, `move_window(id, x, y, w, h)`
  (unmaximizes, then waits until the frame is there; Wayland clients cannot place
  themselves) and `get_pointer()`.
- The venv has no `gi`, so the client runs one bridge process (system `python3` with
  `python3-gi`, inside the session environment) and exchanges JSON lines with it.
- Screenshots are only written inside the session root (`/tmp/dragshift-session-*`); by
  default they go to `<root>/tmp/screenshot-N.png`. Copy one out before the session stops.
- `Session(helper=True)` waits until the helper owns its bus name and answers (the
  bridge watches the name, so there is no polling). The helper logs its steps to the shell
  log as `dragshift-test@eteen12.github.io: …`. If it fails to load (say, a
  syntax error), `start()` raises `HelperError` with the extension state, the shell log
  lines that name it and the shell log tail.

```sh
uv run python -m tests.linux.session --helper -- gdbus call --session \
  --dest io.github.eteen12.DragShiftTest --object-path /io/github/eteen12/DragShiftTest \
  --method io.github.eteen12.DragShiftTest.ListWindows
```

### Pointer and keyboard injection

The helper also injects input into the isolated session: `MovePointer(x, y)`,
`Button(button, pressed)`, `Key(keysym, pressed)`, `TypeText(text)` and `ReleaseAll()`.
It uses Clutter virtual devices on the isolated shell's own seat. They live inside that
gnome-shell process and create no uinput or `/dev/input` device, so the events cannot reach
your real desktop. The extension also refuses to create them unless the shell is headless
and runs from a launcher session root (`/tmp/dragshift-session-*`).

- `tests/linux/inject.py` has the gestures: `glide(shell, x, y, dur)` (at least 60
  smoothstep steps), `click`, `press("Escape")`, `with hold_key(shell, "Shift_L"):`,
  `drag_to(shell, x, y, start=..., hold="Shift_L")`, `type_text` and `release_all`. Keys
  are X keysym names (`Shift_L`, `Control_L`, `Alt_L`, `Escape`, `Return`) or characters.
- Each call returns after the shell has handled its events, so a key release can never
  overtake the move sent before it. It also waits until the seat's modifier mask shows the
  Shift, Control and Alt keys and the buttons held, so the next reader never sees a
  released key as still down.
- `TypeText` types only characters that the session's keymap has. The session uses the
  default `us` layout, which covers ASCII.
- Do not use uinput, `ydotool` or `xdotool` instead: those drive the real seat.
- Mutter clamps coordinates to the monitor. Avoid (0, 0), where GNOME's hot corner opens
  the overview (the Docker image's `user` mode has it on).
- `tests/linux/probe_window.py` is a PySide6 window that logs every event it gets as JSON
  lines, and its `ProbeWindow` runs it in a session. It uses Xwayland (`xcb`) when Qt can
  load that plugin, and native Wayland otherwise. On a desktop without `libxcb-cursor0`
  that means Wayland; the Docker image runs it on xcb.
- The probe also accepts drops from other apps and logs `dragEnter`, `dragMove`,
  `dragLeave` and `drop` with the offered MIME types, `urls()`, the raw `text/uri-list`,
  the proposed, possible and answered actions. By default it answers `CopyAction`, like
  DragShift's menu (`ProbeWindow(..., drop_action="proposed"|"ignore")` changes that).
  `probe.move(x, y, w, h)` puts it somewhere else.
- `tests/linux/test_inject.py` also proves the isolation. The input methods are served by
  the isolated shell's pid on its private bus, no session process holds `/dev/input/*` or
  `/dev/uinput`, and `/proc/bus/input/devices` does not change. The real desktop's idle
  time keeps growing while input is injected; this check is a read-only
  `org.gnome.Mutter.IdleMonitor.GetIdletime` call, skipped without a real session or while
  the desktop is in use.

### Files and desktop icons

`tests/linux/files.py` opens folders in Files (Nautilus), puts files on the DING desktop of
the session's HOME and returns where a named file's icon is on the stage, so tests can
start real drags from there:

```python
files = Files(gnome_session, shell_helper)
folder = files.make_folder("pics", {"Photo.jpg": data})
files.open_folder(folder)            # new window, moved to NAUTILUS_RECT and focused
start = files.icon_center("Photo.jpg", "nautilus")
files.put_on_desktop({"Photo.jpg": data})
start = files.icon_center("Photo.jpg", "desktop")
inject.drag_to(shell_helper, *probe.center(), start=start, hold="Shift_L")
files.close()                        # nautilus -q
```

- Lookups go through AT-SPI: the session's own accessibility bus is activated on its
  private bus. The match is on the exact file name (`Photo.jpg` never matches
  `Photo (1).jpg`). The point is the centre of the item's icon image. A Wayland client does not
  know its window position, so the extents are taken in window coordinates and the
  frame from `ListWindows` is added.
- Without an accessibility bus (or when the app exports no tree) a fallback answers and
  logs a warning: Nautilus is opened in list view and rows come from measured constants,
  and DING icons from the position DING stores in `metadata::nautilus-icon-position`.
  `files.method` says which path answered.
- `icon_center` raises `FilesError` when the icon is missing, scrolled out of its window
  or covered by another window (for desktop icons, by any window).
- Every session seeds Ubuntu's DING layout (icons in the bottom-right corner, no Trash or
  volume icons), so icon positions are the same on the desktop and in the Docker image.
- In `test_files.py`, the drags run against the probe under native Wayland and, where Qt loads
  xcb (the Docker image), under Xwayland. With Shift held, Xwayland's bridge only offers
  `MoveAction`, but the source file stays in both cases. Qt never sees Shift in a drop
  event, because the target has no keyboard focus during a drag.

### Display backend evidence

`tests/linux/test_display_backend.py` backs `docs/linux/display-backend.md`. It shows a
window with the radial menu's flags (`tests/linux/menu_probe.py`) under native Wayland,
placed by the extension's `PlaceWindow`, and under Xwayland, next to Files and the
desktop. It checks placement, stacking, focus, latency, real Shift+drags, `grabWindow`,
the fullscreen-overlay alternative and a 200 % monitor. The xcb cases skip where Qt
can't load xcb. To run them here without installing anything, point
`DRAGSHIFT_XCB_LIBS` at a directory that holds `libxcb-cursor.so.0`, e.g. one extracted
with `apt-get download libxcb-cursor0 && dpkg-deb -x libxcb-cursor0_*.deb <dir>`.

```sh
DRAGSHIFT_XCB_LIBS=<dir>/usr/lib/x86_64-linux-gnu uv run pytest -s -m gnome tests/linux/test_display_backend.py
```

### The same tests in Docker (CI parity)

`tests/linux/docker/run.sh` builds an `ubuntu:24.04` image with GNOME Shell 46, Xwayland,
Nautilus, Ubuntu's dock, AppIndicator and DING extensions, AT-SPI, Python 3.12 and uv, then
runs `pytest -m gnome tests/linux` inside it through the same launcher. The repo is mounted
read-only and the Python packages live in the image, so rebuild after changing
`requirements.txt` (the script always runs `docker build`; unchanged layers are cached).
Extra arguments go to pytest. GL is software (llvmpipe): no `/dev/dri`, no `--privileged`.
The host session above stays the main way to test; the image is for CI and for checking
that nothing depends on this machine.

```sh
tests/linux/docker/run.sh                                     # gnome tests in the image
tests/linux/docker/run.sh -k nautilus -v                      # pytest arguments
DOCKER_BUILD_ARGS="--build-arg WITH_LIBREOFFICE=1" tests/linux/docker/run.sh   # + LibreOffice
```

Without `yaru-theme-gnome-shell` (or on another distro) the launcher boots GNOME's
`user` mode instead of `--mode=ubuntu` and enables the installed Ubuntu default extensions
itself, so both paths give the same desktop.

## Start-up on Linux

`app.main()` calls `linux_qpa()` before `QApplication` exists and `linux_setup(qapp)` right
after (`dragshift/app.py`, #11):

- **Qt platform.** A `QT_QPA_PLATFORM` or `-platform` you set always wins. Otherwise
  `DRAGSHIFT_QPA` picks one (for tests), a Wayland session (`WAYLAND_DISPLAY` set or
  `XDG_SESSION_TYPE=wayland`, and not `x11`) gets `wayland;xcb` (native Wayland, Xwayland as
  Qt's fallback, `docs/linux/display-backend.md`), and an X11 session keeps Qt's default.
  The choice goes to Qt as a `-platform` argument, not as an environment variable, so
  programs DragShift starts (`xdg-open`, LibreOffice) don't inherit it.
- **Fonts.** The Segoe UI families the UI asks for are substituted with `Ubuntu Sans`,
  `Ubuntu`, `Cantarell`, `Noto Sans`, `DejaVu Sans` (the first installed one wins).
- **App id.** `setDesktopFileName("io.github.eteen12.DragShift")`: every window's Wayland
  app id, so GNOME (dock, Alt+Tab, tray) matches it to `io.github.eteen12.DragShift.desktop`.
- **HiDPI.** Nothing extra: with the `PassThrough` rounding policy each window uses the
  monitor's exact scale, also a fractional one, so text is drawn at device pixels.
- The log gets `platform: wayland`, one `screen …` line per monitor and the font Segoe UI
  resolved to.

```sh
uv run pytest tests/test_linux_startup.py                 # unit tests, offscreen
uv run pytest -s tests/linux/test_startup.py              # real app in the session, scale 1, 2, 1.5
```

## Tray (top-bar icon)

The tray is Qt's `QSystemTrayIcon`. On Linux, Qt publishes it as a StatusNotifierItem with
a `com.canonical.dbusmenu` menu. Ubuntu's AppIndicator extension
(`ubuntu-appindicators@ubuntu.com`) shows it in the top bar (#46).

- **Icon.** The item sends pixmaps and no icon name: the 16, 24, 32, 48 and 64 px
  frames of `assets/dragshift.ico`. The top bar draws the smallest frame that is at
  least 16 × the scale, so it draws the 16 px frame at 100 % and the 32 px frame at
  200 %, both 1:1. Yaru's top bar is dark in both colour schemes, so the colour icon
  works in light and dark mode. The 1024 px PNG would arrive as 22 and 64 px frames,
  and the top bar would scale them down.
- **Clicks (AppIndicator 58).** A click opens the menu after the double-click delay.
  A double click sends `Activate`, which Qt reports as `Trigger`, and that opens the
  file picker, like a click on Windows. A right click opens the menu. A middle click
  sends `SecondaryActivate` (`MiddleClick`), which DragShift ignores.
- **Tip the developer ♥** runs `xdg-open`. Qt's `openUrl` would ask "Open with…"
  through the portal.
- **Enabled** writes `tray: trigger enabled` or `tray: trigger disabled` to the log.
- **No StatusNotifier host** (another desktop, or AppIndicator disabled): the app keeps
  running and logs `tray: no StatusNotifierWatcher; …`. While no host is there,
  opening DragShift again opens Settings instead of the "already running" toast, and
  the start-up toast says so. Qt registers the item by itself when a host appears
  later.

```sh
uv run pytest tests/test_linux_tray.py              # offscreen
uv run pytest -s tests/linux/test_tray.py           # real app and top bar in the session
```

## Open With and the command line

`dragshift a.jpg b.jpg` (or `python -m dragshift a.jpg b.jpg`) hands the files to the
running DragShift and exits. That DragShift opens one click-mode menu for all of them. If
DragShift is not running, the same command starts it with the files.

- The hand-off happens in `dragshift/handoff.py` before Qt is imported: it sends
  `{"files": [...]}` (or `{"show": true}` with no files) to the app's `QLocalServer` and
  takes about 20–35 ms.
- On Linux the socket is `$XDG_RUNTIME_DIR/DragShift`. Only you can create files in your
  runtime dir, so another local user cannot take the name first, as they could in
  `/tmp`. Without a private (0700, owned by you) runtime dir it falls back to the old
  `$TMPDIR/DragShift-$USER`. Windows (named pipe) and macOS (temp dir) are unchanged.
- A socket left by a crash or a SIGTERM: nothing accepts on it, so the hand-off fails
  at once and the new process starts the app, whose `QLocalServer.removeServer` clears it.
- Relative paths are resolved against the caller's working directory, and paths that do
  not exist are dropped. Launches within 280 ms are merged into one menu.

`packaging/linux/` holds what install-linux.sh (#66) installs:
- `io.github.eteen12.DragShift.desktop`: named after the app id, so GNOME links
  DragShift's windows to it. `Exec=dragshift %F` and a `MimeType` for every file type
  DragShift handles put it under *Open With* in Files. Check it with
  `desktop-file-validate`.
- `dragshift`: the `~/.local/bin/dragshift` launcher. It ends in
  `exec "$VENV/bin/python" -m dragshift "$@"`, the form the extension's caller check
  accepts. Keep it that way.

`tests/linux/test_handoff.py` installs both into a session's HOME and XDG_DATA_HOME,
never into yours. It covers `gio mime`, the CLI with the app running and not running,
and Files → Open With… → DragShift.

### Files: right-click → Scripts → DragShift…

`packaging/linux/nautilus-scripts/DragShift…` is a Nautilus script. The installer copies
it to `${XDG_DATA_HOME:-~/.local/share}/nautilus/scripts/`, and its file name is the
menu label. It runs `~/.local/bin/dragshift` with the selected files, so one menu opens
at the pointer. If `~/.local/bin/dragshift` is missing, it runs `dragshift` from `PATH`,
or `python -m dragshift` from a checkout named by `DRAGSHIFT_SRC`.
- **Where the files come from.** Nautilus 46 and the desktop icons (DING) pass each
  selected file as one argument, so names with spaces, unicode or newlines arrive
  intact.
  - Nautilus passes names relative to the folder and runs the script in that folder.
    The script makes them absolute.
  - Without arguments, the script decodes `NAUTILUS_SCRIPT_SELECTED_URIS`, which is
    also exact. Then it tries `NAUTILUS_SCRIPT_SELECTED_FILE_PATHS`, one path per line,
    where a name containing a newline is lost.
- **With nothing selected,** the script runs the launcher without files, which brings
  DragShift up. Nautilus 46 shows Scripts only when files are selected, though, not on a
  folder's background.
- **Tests:**
  - `tests/test_nautilus_script.py` runs the script under sh, dash, bash and busybox
    with a fake launcher. It also runs the whole chain to a fake socket.
  - `tests/linux/test_nautilus_script.py` drives the menus in a session with the
    pointer.
  - Neither touches your own `~/.local/share/nautilus/scripts`.

## DOCX → PDF and LibreOffice

On Linux, DOCX → PDF uses LibreOffice when it is installed (`soffice` or `libreoffice` on
`PATH`, the snap at `/snap/bin/libreoffice`, or the flatpak `org.libreoffice.LibreOffice`) and
falls back to the built-in renderer otherwise. Each conversion runs headless with a throwaway
profile and a copy of the source, in `~/snap/<snap>/common/DragShift/` for the snap (it can't
use `/tmp` or hidden folders such as `~/.cache`) and in `$XDG_CACHE_HOME/DragShift/soffice/`
otherwise. The per-conversion folder is removed afterwards; your own LibreOffice profile is
never used.

The unit tests hide an installed LibreOffice from every test except `tests/test_soffice.py`.
The tests marked `libreoffice` run the real one (about 10 s with the snap) and are skipped when
it is missing. To leave them out:

```sh
.venv/bin/python -m pytest -q -m "not libreoffice"
```

## GNOME Shell extension

The app gets the global pointer and screen capture on Wayland from its own GNOME Shell 46
extension, `extension/dragshift@eteen12.github.io/`. It owns `io.github.eteen12.DragShift`
on the session bus, and its interface is in `dbus.xml`. `dragshift/platform_linux.py` is
the Python client (`jeepney`, 200 ms timeout, `None` when the extension doesn't answer).
`PlaceWindow` (in `placement.js`, client `platform_linux.place_window`) places, raises and
un-focuses the caller's own windows, since a Wayland client can't do that itself.
`GetWorkArea(x, y)` (client `platform_linux.work_area`) returns the work area (monitor
minus the top bar and a fixed dock) of the monitor at a point. Under Wayland Qt's
`availableGeometry()` is the whole monitor, so toasts stack inside this instead (#42):

```sh
uv run pytest -s tests/test_toast_stack.py tests/linux/test_toasts.py   # scale 1 and 2, two monitors
```

```sh
extension/build.sh                 # → dist/dragshift@eteen12.github.io.shell-extension.zip
uv run pytest tests/linux/test_extension.py   # build + client tests, then session tests
```

`build.sh` writes the app version from `dragshift/__init__.py` into the zip's
`metadata.json` (`version-name`), and the extension's `Version()` returns it. At start-up
the app prints a warning when the two differ. The zip is what `gnome-extensions install
--force` takes, but GNOME on Wayland only loads a newly installed extension after you log
out and back in. To try a change, load it in the isolated session rather than on your
desktop:

```sh
mkdir -p /tmp/ext && unzip -o -d /tmp/ext/dragshift@eteen12.github.io dist/*.zip
uv run python -m tests.linux.session --extension /tmp/ext/dragshift@eteen12.github.io -- \
  sh -c 'gdbus wait --session --timeout 10 io.github.eteen12.DragShift &&
    gdbus call --session --dest io.github.eteen12.DragShift \
      --object-path /io/github/eteen12/DragShift --method io.github.eteen12.DragShift.Version'
```

The launcher returns once the shell answers on D-Bus. Extensions are enabled a moment
later, so wait for the name before calling it.

### Who may call the extension (trust model)

Everything but `Version` gives out what GNOME withholds from ordinary apps: screen pixels
(`CaptureArea`, `CaptureAreaExcludingCaller`), the global pointer with the Shift/Ctrl/Alt
and button state (`GetPointer`), a global Escape grab (`WatchEscape`), window placement
(`PlaceWindow`) and the monitors' work areas (`GetWorkArea`). GNOME 41+ allowlists its own screenshot API for that reason, so these
methods answer only the DragShift app (#93). `Version` stays open, so the app and the
installer can find the extension. `caller.js` decides:

- The bus daemon gives the caller's uid and pid (`GetConnectionCredentials`). The uid must
  be the shell's own.
- `/proc/<pid>/exe` must be a Python interpreter, and `/proc/<pid>/cmdline` must run the
  app: `python -m dragshift`, `python .../dragshift/__main__.py`, or the installed launcher
  `~/.local/bin/dragshift` (#66; it execs `python -m dragshift`). `python run.py` is not
  accepted: start the app with `python -m dragshift` (the app prints a hint when it is
  refused).
- Anyone else gets `org.freedesktop.DBus.Error.AccessDenied`, and the shell log (journal)
  gets one `DragShift: refused <method> to <bus name> (pid N): <reason>` line per
  connection.
- The verdict is cached per unique bus name and dropped when that name leaves the bus
  (`NameOwnerChanged`). The same notification ends a caller's Escape grab and drops its
  window placements. So only the first call of a connection pays for the check (about
  0.9 ms). After that `GetPointer` takes a median 0.20 ms, the same as the unchecked
  `Version` (0.19 ms; `tests/linux/test_caller_check.py::test_get_pointer_latency`).

What this does and doesn't protect against:

- It keeps out casual and accidental callers: `gdbus call`, scripts, and other programs of
  the user that would otherwise screenshot the screen or follow the pointer quietly.
- It is defence in depth, not a security boundary against code that already runs as
  you. A same-user process can start Python with DragShift's command line (even a fake
  `dragshift` package of its own), or attach to the real app with a debugger, and then
  call everything. The tests show this on purpose. Such a process could also read your
  files directly.
- Other users can't reach your session bus. Flatpak and snap apps don't see
  `io.github.eteen12.DragShift` at all, because their D-Bus proxies only pass allowlisted
  names, so the check isn't what keeps them out.
- The pid comes from the bus daemon at connection time. A process that `exec`s
  something else keeps its connection and its verdict.

Tests run as pytest, not as DragShift. Only in a headless shell started by the test
launcher (session root `/tmp/dragshift-session-*`, the same guard as the test helper's
input injection) does the extension also let in pytest, `python -m tests.linux.<module>`
and scripts in a `tests/linux` directory. `python -c`, other scripts and `gdbus` are
refused there too, so a test that drives app code in a process of its own puts it in a
module (e.g. `python -m tests.linux.toast_driver`). `tests/linux/caller.py` calls every method, and the tests run it under
different command lines:

```sh
uv run pytest tests/linux/test_caller_check.py -s   # refusals, the real app, latency
```

### Escape while the menu is open

During a file drag, Mutter's drag grab takes Escape itself and cancels the drag, so
no app or extension sees the key. What the drop target gets instead is a `dragLeave`
about 1-2 ms after Escape while the left button is still down (`pointer_state()` still
reports it), and no drop when the button goes up. That is how the app learns about a
cancelled drag (#32). Measured with real drags from Files and the desktop into a
probe window, under native Wayland and Xwayland (`tests/linux/test_escape.py`).

The drag-mode menu acts on it (#32). `RadialMenu.dragLeaveEvent` records when a file
drag left (`drag_cancelled_at`), and `drag_cancelled_since(pos)` reports that time only
while the pointer is still over the menu. The watcher closes the menu once that has
lasted `trigger.DRAG_CANCEL_GRACE` (150 ms) with the button still down and no drop or
re-entry. That takes about 155-180 ms after Escape. A file carried out of the menu
leaves with the pointer outside, so the menu stays until the button goes up, as on
Windows. Tested with the real app in `tests/linux/test_menu_cancel.py`.

Outside a drag (the menu's click mode), `platform_linux.watch_escape(True, callback)`
asks the extension (`WatchEscape`) to grab Escape globally and calls `callback` on
every press (`EscapePressed`), from a background thread. While the grab is held the
focused window gets no key press, so Qt's `keyPressEvent` can't fire as well.
`watch_escape(False)` releases it. Call it only while the menu is open: Escape is taken
from every other app meanwhile. The grab belongs to the client's own D-Bus connection,
so it also ends when the app exits or crashes, or when the extension is disabled. It
returns False when the extension is missing or something else already holds Escape.

### Click mode (menu without a drag)

The tray's *Open a file…*, Files' Open With and a second `dragshift <file>` open the menu
in click mode (`app.show_click_menu`, #36). On Linux:

- **Where it opens.** The menu opens at the pointer from the extension
  (`app.linux_click_pos`). Without the extension it opens at the centre of the primary
  screen. `activateWindow()` is not called, since on Wayland it can only ask for
  attention.
- **Escape.** The menu never has the keyboard focus (the extension hands it back), so
  it holds `watch_escape(True)` while it is open and releases it when it hides.
- **Clicks outside.** `grabMouse()` is not used on Linux. Under Wayland it only
  redirects events between DragShift's own windows. Measured: 0 of 70 clicks on other
  windows reached the grabbing menu. Instead, a thread reads `pointer_state()` every
  `radial.CLICK_POLL_MS` (8 ms) while the menu is open. A button going down outside the
  menu's frame closes the menu, on the press. Shift going down flips the ring.
  - A GUI-thread timer stalled for up to 60 ms while the menu painted, so the poll has
    its own thread.
  - Measured with the real app on this machine (20 tries each, under the load of
    parallel test sessions): presses held 20 ms or more closed it every time,
    10-15 ms in 39 of 40 tries, 6 ms in 19 of 20, and instant press/release pairs in
    13 of 20. A human click holds 30 ms or more.
  - Nothing grabs the click, so it also reaches what is under it. A right-click on the
    desktop closes the menu and opens the desktop's context menu.
- **Clicks on the menu** still come to `mouseReleaseEvent`: a petal runs its action, the
  hub flips the ring, and a right-click or a click on the transparent corners closes the
  menu.
- **File picker.** `QFileDialog.getOpenFileNames` stays as it is.
  - By default (no `QT_QPA_PLATFORMTHEME`, GNOME), Qt's GTK 3 theme shows GTK's chooser
    inside DragShift's process. Its Wayland app id is `python`, so the dock shows it as
    a separate window.
  - With `QT_QPA_PLATFORMTHEME=xdgdesktopportal`, GNOME's portal chooser
    (`xdg-desktop-portal-gnome`) is used.
  - Both return the real path for an unsandboxed app.

```sh
uv run pytest tests/test_click_mode.py                        # offscreen, extension faked
uv run pytest -s tests/linux/test_click_mode.py               # real app, scale 1 and 2
```

### Drops onto the menu

A Shift+drag from Files or the desktop opens the menu under the pointer. The file then
enters it, and the drop on a petal runs that action (`activated` → `run_item`). The
result is written next to the original (#35).

- **Paths.** They are read from `text/uri-list` when the drag enters, because the
  desktop's drop can arrive without a payload. `QUrl.toLocalFile` decodes the
  percent-encoding, so names with spaces, `#`, `%` or non-ASCII letters arrive intact.
  URIs that are not local files (`sftp://`, `smb://`, `https://`) are ignored. A drag
  that has only those is refused.
- **Copy, never Move.** The source app proposes Copy or Move. Files proposed
  `MoveAction` in about half of the measured Shift+drags, and the desktop proposed
  `CopyAction`. Both offer Copy, and the menu answers `CopyAction` on enter, move and
  drop, so the original stays where it is, unchanged. On Linux the drop is logged as
  `menu: dropped N file(s) on <petal> (<ring>), proposed …, answered CopyAction`.
- **Beside the petals** (the corners, the hub) the drop is refused and the menu closes.
  The same happens for a file type that has no petals. Nothing runs.
- **Rings.** A Shift tap flips the ring through the watcher (`pointer_state()`), since
  Qt never sees Shift during a drag. Resting on the hub for 420 ms flips it too, but only
  after the pointer has moved onto a petal. On GNOME the menu is clamped to the work
  area, and desktop icons sit at the right edge, so the file often enters on a petal.
  That petal does not count as a visit.

```sh
uv run pytest tests/test_menu_drop.py                         # offscreen, synthetic drops
uv run pytest -s tests/linux/test_menu_drop.py                # real drags from Files and the desktop
```

### Screen capture (`CaptureArea`)

`platform_linux.capture(x, y, w, h)` returns the screen under a rect as a PIL RGB image of
exactly w×h, or None when the extension doesn't answer. It comes from the extension's
`CaptureArea`, which paints the rect offscreen with the drag icon hidden: Mutter's
feedback group (Wayland drags, e.g. from Files and the desktop) and DND-type windows (X11
drags). The paint is synchronous, so the icon is back before the next frame. The cursor
is never in it. A rect partly off the screen is clamped by the extension and padded with
edge pixels by the client.

Almost all of the time goes into the shell's PNG encoding, so `CaptureArea` returns the
rect as up to four horizontal PNG strips encoded in parallel. A 400×400 capture takes
about 11-17 ms end to end (one strip took 24-42 ms). Raw pixels can't be had instead:
GJS copies the buffer passed to `Clutter.Stage.paint_to_buffer` and `Cogl.Texture.get_data`,
so nothing written into it reaches JavaScript.

```sh
uv run pytest tests/linux/test_capture.py -s   # incl. a real Nautilus drag; prints timings
```

#### Behind DragShift's own windows (`exclude_self`)

`capture(..., exclude_self=True)` also leaves out every window of the calling process,
so a visible toast or tool window can re-capture what is behind it (`glass.grab_screen`
with `below_window` set, which `LiquidBacking` uses for a visible window). It calls
`CaptureAreaExcludingCaller`. The extension asks the bus daemon for the caller's pid
(`GetConnectionCredentials`, in the caller check above) and hides every window actor
whose `Meta.Window.get_pid()` matches. That works for native Wayland and Xwayland windows alike. The windows are hidden
in the same synchronous paint as the drag icon, so they never leave the screen. A
minimised window stays minimised. The app sends no pid or window id, so it can't hide
another app's windows. An extension without the method gives None, and `grab_screen`
falls back to the wallpaper.

The windows and the capture must be in one process, so `tests/linux/own_window.py` runs
both inside the session:

```sh
uv run pytest tests/linux/test_capture_exclude.py -s   # own windows over Files; timings
```

#### Glass windows and toasts: where they are, and live refresh (#40)

Under Wayland a client is never told where its windows are: Qt's `frameGeometry()` is
only what it asked for (`(0, 0)` for a window Mutter placed), so a backdrop captured there
shows the wrong part of the screen.

- **Toasts** are placed by the app (`PlaceWindow`), so `ToastCard.stage_rect()` is the
  top-left it last asked for. The backdrop is captured before the card shows and again,
  with DragShift's windows left out, after it slides into a new slot.
- **Glass windows** (Settings, Welcome, the tools) are placed by Mutter and moved by the
  user with a system move, which sends the app no event. `GlassWindow` asks the extension
  where its frame is: `GetOwnWindowFrame(s title) -> (b mapped, iiii frame)` (client
  `platform_linux.window_frame`). It looks only among the caller's own windows (by pid,
  like `CaptureAreaExcludingCaller`), so glass window titles are unique per process
  (`Crop Image (2) — DragShift`). The window polls it every 100 ms while shown (about
  0.4 ms a call), backing off to 1.6 s while it is unknown. The first answer captures the
  backdrop; a change, a resize or coming back to the window refreshes it 140 ms after it
  settles. Each refresh is logged as `liquid: refreshed WxH at (x, y) from capture: ...`.
- Without the extension both use the painted frost and nothing is polled or captured.

A refresh runs `gc.collect()`, the capture (both on the GUI thread) and the render (a pool
thread). On this machine: about 15 ms, 35 ms (scale 1) or 150 ms (scale 2), and 80 ms for
an 820x680 window. `tests/linux/glass_app.py` runs the real `CropWindow` and `ToastStack`
in one process inside the session and records every capture, so the test can compare each
backdrop with the screen behind the window:

```sh
uv run pytest tests/test_window_glass.py tests/linux/test_window_glass.py -s   # scale 1 and 2; prints timings
```

## End-to-end runner

`demo/e2e_linux.py` is the Linux twin of `demo/e2e_windows.py` (#57). It boots its own
isolated session (the launcher above, with DragShift's extension built by
`extension/build.sh` and the test helper), starts the app from this Python, makes the sample
files in the session HOME (`~/DragShiftTest`, and `~/Desktop/D-*` when a desktop scenario
runs) and opens them in Files. Each scenario is a real Shift+drag injected into that session:
press on the icon, move 72 px, Shift, into the menu, a Shift tap for the Tools ring, onto
the petal, release. Then it checks the app logged the drop on that petal, verifies the
output with the shared verifiers and checks that the dragged files kept their bytes and
mtime. Nothing goes into your HOME.

```sh
uv run python demo/e2e_linux.py                       # every scenario
uv run python demo/e2e_linux.py --only "JPG → PNG"    # one (repeat --only for more)
uv run python demo/e2e_linux.py --list
uv run python demo/e2e_linux.py --record --out /tmp/e2e   # + e2e_linux.mp4 (.webp without ffmpeg)
uv run pytest tests/linux/test_e2e_smoke.py           # the runner with --only "JPG → PNG"
```

- Output (`e2e_linux_out/` or `--out`): `results.json` (`{name, where, status, detail,
  seconds, t}`), `results.md` (the Windows table), `log.txt`, `app_log.txt` (the app's own
  output, where the `menu:` and `trigger:` lines go, then its `log.txt`), `shots/`
  (`NN_<name>_menu.png` over the petal, `NN_FAIL_<name>.png`). The exit code is 1 when a
  scenario is not PASS or none ran.
- A failed scenario takes a screenshot, presses Escape, releases every key and button,
  puts back any sample file that went missing, and the run continues.
- `--record` takes a screenshot every 0.5 s, with a dot for the pointer (the helper's
  screenshots leave the cursor out). GNOME's own screencast isn't available in the
  isolated session.
- To add a scenario, append a `Scenario(...)` to `SCENARIOS` at the end of the file. It
  takes the arguments of `e2e_windows.run()`, with `where="nautilus"` or `"desktop"`.
  `tool` drives a tool window after the drop.
- The sample files, `new_files` and the `v_*` verifiers live in `demo/e2e_common.py`, which
  the Windows and macOS runners import too.
- In the Docker image the repo is read-only, so pass `--out` on a writable mount:

```sh
docker run --rm --init --shm-size=512m --user "$(id -u):$(id -g)" -v "$PWD:/src:ro" \
  -v /tmp/e2e:/out -w /src dragshift-gnome-test:local python demo/e2e_linux.py --out /out
```
