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
refuses to load the `xcb` platform plugin.

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
`ListWindows`, `ActivateWindow(id)`, `GetPointer` and the input methods below.

- `Session(helper=True)` (CLI: `--helper`) copies it into the session's temporary
  `XDG_DATA_HOME` and enables it. The shared `gnome_session` fixture always does this.
  Nothing installs it anywhere else, and it must never go on a real desktop.
- `tests/linux/shell.py` is the client. The `shell_helper` fixture gives a ready
  `ShellHelper`: `screenshot()`, `screenshot_area(...)`, `list_windows()`,
  `wait_for_window(wm_class=...)`, `activate_window(id)`, `get_pointer()`.
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
- `tests/linux/test_inject.py` also proves the isolation. The input methods are served by
  the isolated shell's pid on its private bus, no session process holds `/dev/input/*` or
  `/dev/uinput`, and `/proc/bus/input/devices` does not change. The real desktop's idle
  time keeps growing while input is injected; this check is a read-only
  `org.gnome.Mutter.IdleMonitor.GetIdletime` call, skipped without a real session or while
  the desktop is in use.

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
