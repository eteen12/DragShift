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
- `Session(helper=True)` waits until the helper answers. If it fails to load (say, a
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
  overtake the move sent before it.
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

### Escape while the menu is open

During a file drag, Mutter's drag grab takes Escape itself and cancels the drag, so
no app or extension sees the key. What the drop target gets instead is a `dragLeave`
about 1-2 ms after Escape while the left button is still down (`pointer_state()` still
reports it), and no drop when the button goes up. That is how the app learns about a
cancelled drag (#32). Measured with real drags from Files and the desktop into a
probe window, under native Wayland and Xwayland (`tests/linux/test_escape.py`).

Outside a drag (the menu's click mode), `platform_linux.watch_escape(True, callback)`
asks the extension (`WatchEscape`) to grab Escape globally and calls `callback` on
every press (`EscapePressed`), from a background thread. While the grab is held the
focused window gets no key press, so Qt's `keyPressEvent` can't fire as well.
`watch_escape(False)` releases it. Call it only while the menu is open: Escape is taken
from every other app meanwhile. The grab belongs to the client's own D-Bus connection,
so it also ends when the app exits or crashes, or when the extension is disabled. It
returns False when the extension is missing or something else already holds Escape.

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
(`GetConnectionUnixProcessID`) and hides every window actor whose `Meta.Window.get_pid()`
matches. That works for native Wayland and Xwayland windows alike. The windows are hidden
in the same synchronous paint as the drag icon, so they never leave the screen. A
minimised window stays minimised. The app sends no pid or window id, so it can't hide
another app's windows. An extension without the method gives None, and `grab_screen`
falls back to the wallpaper.

The windows and the capture must be in one process, so `tests/linux/own_window.py` runs
both inside the session:

```sh
uv run pytest tests/linux/test_capture_exclude.py -s   # own windows over Files; timings
```
