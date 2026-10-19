# Testing DragShift on Linux

Ubuntu 24.04, GNOME Shell 46, Python 3.12, from the repo root. How the Linux parts work
is in [LINUX-DEV.md](LINUX-DEV.md).

## Setup

Install the system packages listed in [LINUX-DEV.md](LINUX-DEV.md#system-packages), then:

```sh
uv venv .venv
uv pip install -r requirements.txt pytest
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest`,
then use `.venv/bin/python -m pytest` wherever this guide says `uv run pytest`.

## Quick unit loop

```sh
uv run pytest -m "not gnome"                     # about 1 min, no session
uv run pytest -m "not gnome and not libreoffice" # also skip the real LibreOffice (~10 s each)
uv run pytest tests/test_menu_drop.py            # one file
```

`tests/conftest.py` makes every test safe to run on your desktop:
- `QT_QPA_PLATFORM=offscreen` unless you set it, so nothing shows on screen.
- `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` point at a
  temporary folder, so `~/.config/DragShift` and `~/.local` are never touched.
- A session bus address that doesn't exist, so a test can't reach your desktop over D-Bus
  (not for `gnome` and `libreoffice` tests).
- An installed LibreOffice is hidden from every test except `tests/test_soffice.py`.

`uv run pytest` with no marker runs everything, the session tests included.

## The isolated GNOME session

Most Linux behaviour (the extension, the pointer, drags from Files, window placement,
the tray) only exists inside GNOME Shell. Testing it on your own desktop would install
extensions, move your windows and inject input into your seat. So the tests boot their own
throwaway headless GNOME Shell with `tests/linux/session.py` and run everything in there.

What the launcher isolates:
- its own HOME, `XDG_*` directories and `TMPDIR` under `/tmp/dragshift-session-*`, so
  an app in the session can't take your DragShift's IPC socket;
- a short `XDG_RUNTIME_DIR` (`/tmp/dsr.*`), its own session bus and an empty private
  system bus (no GeoClue, logind or PackageKit side effects);
- a private copy of DING (desktop icons), because DING kills every other DING of the same
  user when it starts;
- the child environment is built from a whitelist, so your `DISPLAY`, `WAYLAND_DISPLAY`
  and `DBUS_SESSION_BUS_ADDRESS` never leak in.

Teardown kills every process that carries the session's runtime dir and deletes the
temporary directories, also on Ctrl-C. A session whose launcher was SIGKILLed is cleaned up
by the next start.

Never run `gnome-shell --headless` by hand: without the launcher it restarts your desktop
icons and talks to the real system bus. Never start DragShift on your desktop to test it;
start it in a session.

```sh
uv run python -m tests.linux.session -- gnome-extensions list    # run one command inside
uv run python -m tests.linux.session --helper -- gdbus call --session \
  --dest io.github.eteen12.DragShiftTest --object-path /io/github/eteen12/DragShiftTest \
  --method io.github.eteen12.DragShiftTest.ListWindows
uv run python -m tests.linux.session      # print the session env; Ctrl-C to stop
```

Options: `--size WxH` (default 1600x900), `--scale 2` (with `--size 3200x1800` for
1600x900 logical, like a 200 % screen; `--scale 1.5` is fractional), `--extension DIR`
(install and enable an extension; repeatable), `--helper` (the test-helper extension) and
`--keep` (keep the temporary directories). The launcher returns once the shell has
finished starting up; extensions are enabled a moment later, so wait for their bus name.

To try DragShift's own extension and the app in a session:

```sh
extension/build.sh
ext=$(mktemp -d)/dragshift@eteen12.github.io
unzip -q -d "$ext" dist/dragshift@eteen12.github.io.shell-extension.zip
uv run python -m tests.linux.session --extension "$ext" -- \
  sh -c 'gdbus wait --session --timeout 10 io.github.eteen12.DragShift &&
    gdbus call --session --dest io.github.eteen12.DragShift \
      --object-path /io/github/eteen12/DragShift --method io.github.eteen12.DragShift.Version'
uv run python -m tests.linux.session --size 3200x1800 --scale 2 \
  --extension "$ext" -- python -m dragshift   # Ctrl-C to stop
```

## Session tests

Tests marked `gnome` use the session. They need `gnome-shell`, `dbus` (`dbus-run-session`,
`dbus-daemon`, `dbus-update-activation-environment`), `gdbus` and `python3-gi`, which a
normal Ubuntu desktop has, and are skipped without them.

```sh
uv run pytest -m gnome tests/linux                 # all of them (about 20 min)
uv run pytest -s tests/linux/test_menu_drop.py     # one file, with its timings and log lines
```

### Writing one

- `gnome_session` is one session shared by the whole run, with the test helper.
  `gnome_session.env()` is the environment for child processes; `run(cmd)` and
  `popen(cmd)` run inside it; `log_path` is the shell log. A test that needs another size,
  scale or extension starts its own `Session(size=..., scale=..., extensions=[dir],
  helper=True, data_files={...})` as a context manager.
- `shell_helper` is a ready `ShellHelper` (`tests/linux/shell.py`) for the test-only
  extension `dragshift-test@eteen12.github.io` (`tests/linux/ext/`): `screenshot()`,
  `screenshot_area(...)`, `list_windows()`, `wait_for_window(wm_class=...)`,
  `activate_window(id)`, `move_window(id, x, y, w, h)`, `get_pointer()`.
- `tests/linux/inject.py` drives the session's own seat through the helper: `glide`,
  `click`, `press("Escape")`, `with hold_key(shell, "Shift_L"):`, `drag_to(shell, x, y,
  start=..., hold="Shift_L")`, `type_text`, `release_all`. Keys are X keysym names. Each
  call returns after the shell has handled it. Avoid (0, 0): GNOME's hot corner opens the
  overview. Never use uinput, `ydotool` or `xdotool`: they drive your real seat.
- `tests/linux/files.py` opens folders in Files, puts files on the session's desktop and
  finds a file's icon on screen (`Files(gnome_session, shell_helper).icon_center("Photo.jpg",
  "nautilus")`), so a test can start a real drag from it. Every session seeds Ubuntu's
  desktop-icon layout (bottom-right, no Trash), so positions match the Docker image.
- `tests/linux/probe_window.py` is a window that logs every event and drop it gets, for
  checking what the shell delivers.
- Code that calls DragShift's extension must run as pytest or as a module under
  `tests/linux` (`python -m tests.linux.toast_driver`), not `python -c`; see
  [the caller check](#accessdenied-from-dragshifts-extension).
- Change the monitors (scale, size) only after `start()` returns. A change during the
  shell's startup wedges it (#106).

### What covers what

| Area | Offscreen (`tests/`) | In the session (`tests/linux/`) |
|---|---|---|
| Platform module, autostart, wording | `test_linux.py`, `test_linux_autostart.py`, `test_linux_wording.py` | |
| Start-up (Qt platform, fonts, HiDPI) | `test_linux_startup.py` | `test_startup.py` |
| Session, helper, input, Files | | `test_session.py`, `test_helper_ext.py`, `test_inject.py`, `test_files.py` |
| Extension, pointer, caller check | | `test_extension.py`, `test_pointer.py`, `test_caller_check.py` |
| Trigger | `test_trigger_linux.py` | `test_trigger.py` |
| Menu placement, Escape, cancel | `test_menu_cancel.py` | `test_menu_place.py`, `test_escape.py`, `test_menu_cancel.py` |
| Drops on the menu, click mode | `test_menu_drop.py`, `test_click_mode.py` | `test_menu_drop.py`, `test_click_mode.py` |
| Capture and glass | `test_wallpaper.py`, `test_window_glass.py` | `test_capture.py`, `test_capture_exclude.py`, `test_menu_glass.py`, `test_window_glass.py` |
| Toasts and their actions | `test_toast_stack.py`, `test_result_actions.py` | `test_toasts.py`, `test_toast_actions.py` |
| Tray | `test_linux_tray.py` | `test_tray.py` |
| Open With, CLI, Files script | `test_handoff.py`, `test_nautilus_script.py` | `test_handoff.py`, `test_nautilus_script.py` |
| LibreOffice, HEIC/AVIF | `test_soffice.py` (`-m libreoffice`), `test_formats.py` | |
| Display-backend evidence | | `test_display_backend.py` |
| E2E runner | `test_e2e_common.py` | `test_e2e_smoke.py` |

## Docker

`tests/linux/docker/run.sh` builds an `ubuntu:24.04` image with GNOME Shell 46, Xwayland,
Files, Ubuntu's dock, AppIndicator and DING, AT-SPI and Python 3.12, and runs
`pytest -m gnome tests/linux` in it through the same launcher. Use it to check that
nothing depends on your machine; the host session stays the main way to test.

```sh
tests/linux/docker/run.sh                          # gnome tests in the image
tests/linux/docker/run.sh -k nautilus -v           # extra arguments go to pytest
DRAGSHIFT_TEST_IMAGE=dragshift-gnome-test:libreoffice \
  DOCKER_BUILD_ARGS="--build-arg WITH_LIBREOFFICE=1" tests/linux/docker/run.sh   # + LibreOffice
```

- The repo is mounted read-only and the Python packages live in the image. The script
  always runs `docker build`; unchanged layers are cached, so a change to
  `requirements.txt` rebuilds.
- GL is software (llvmpipe): no `/dev/dri`, no `--privileged`.
- The image has `libxcb-cursor0`, so its `xcb` tests run instead of skipping.
- `DRAGSHIFT_TEST_IMAGE` sets the image tag (default `dragshift-gnome-test:local`). Give a
  variant build its own tag, so it doesn't replace the default image.

## End-to-end runner

`demo/e2e_linux.py` is the Linux twin of `demo/e2e_windows.py`. It boots its own session
with DragShift's extension (built by `extension/build.sh`) and the test helper, starts the
app, makes the sample files in the session HOME and opens them in Files. Each scenario is a
real Shift+drag into the menu and a drop on a petal. It then checks the app logged the
drop, verifies the output and checks the dragged files kept their bytes and mtime.

```sh
uv run python demo/e2e_linux.py --list                    # scenarios, grouped by file
uv run python demo/e2e_linux.py --only "JPG → PNG"        # one (repeat --only for more)
uv run python demo/e2e_linux.py                           # every scenario
uv run python demo/e2e_linux.py --record --out /tmp/e2e-rec   # + e2e_linux.mp4 (.webp without ffmpeg)
uv run pytest tests/linux/test_e2e_smoke.py               # the runner itself
```

The exit code is 1 when a scenario is not PASS or none ran. A failed scenario takes a
screenshot, presses Escape, releases every key and button, restores any missing sample file
and the run goes on.

In Docker (after `tests/linux/docker/run.sh` has built the image), pass `--out` on a
writable mount and run as your own user (the image's `tester` is uid 1001 and `/src` is
read-only):

```sh
out=$(mktemp -d)
docker run --rm --init --shm-size=512m --user "$(id -u):$(id -g)" -v "$PWD:/src:ro" \
  -v "$out:/out" -w /src dragshift-gnome-test:local python demo/e2e_linux.py --out /out
ls "$out"
```

### Adding scenarios

Put a group of scenarios in its own `demo/e2e_linux_<group>.py` with a `SCENARIOS` list.
The runner imports every `demo/e2e_linux_*.py` when it starts, sorted by file name, after
its own `SCENARIOS`. Separate modules keep parallel branches from editing the same list.

```python
# demo/e2e_linux_conversions.py
from e2e_common import v_format
from e2e_linux import Scenario

SCENARIOS = [
    Scenario("JPG → WEBP", "nautilus", ["Photo.jpg"], "Convert", "WEBP", v_format("WEBP", ".webp")),
]
```

- `Scenario(name, where, files, ring, label, verify, tool=None, select_extra=False)` takes
  the arguments of `e2e_windows.run()`. `where` is `"nautilus"` or `"desktop"`.
- `tool` drives a tool window after the drop. Find the window by title and
  `wm_class == "io.github.eteen12.DragShift"`, not by pid (under the launcher the app's
  windows report gnome-shell's pid).
- `C`, `inject` and the other helpers of `e2e_linux` can be imported. Sample files, `new_files`
  and the `v_*` verifiers are in `demo/e2e_common.py`, shared with Windows and macOS.
- A module that fails to import, has no `SCENARIOS` list or repeats a scenario name stops
  the run with exit code 1.

## CI

`.github/workflows/linux.yml` runs on `ubuntu-24.04` for pull requests and pushes to
`main`: it installs the "Unit tests" and "Qt: offscreen" packages from LINUX-DEV.md, runs
`python -m pytest -q` (the `gnome` tests skip: the runner has no GNOME Shell) and
`python -m dragshift --selftest`. Changes to `**.md` and `docs/**` alone don't run it.
Keep its apt list in sync with LINUX-DEV.md. `build.yml` runs the same unit tests on
Windows and macOS, where `tests/linux/` is not collected.

## Where results land

| What | Where |
|---|---|
| pytest's temporary XDG folders | `/tmp/dragshift-tests-*`, removed at the end |
| A session (HOME, logs, screenshots) | `/tmp/dragshift-session-*`, removed on stop unless `--keep` |
| Shell log | `gnome_session.log_path`; a failed start includes its tail in the error |
| Helper screenshots | `<session root>/tmp/screenshot-N.png`; copy them out before the session stops |
| Screenshots from session tests | `DRAGSHIFT_EVIDENCE_DIR=<dir>` (most `tests/linux` files support it) |
| E2E runner | `e2e_linux_out/` or `--out`: `results.md`, `results.json`, `log.txt`, `app_log.txt`, `shots/` |

## Troubleshooting

### `Could not load the Qt platform plugin "xcb"`

Qt 6.5+ needs `libxcb-cursor0` for `xcb`, and a default Ubuntu 24.04 desktop doesn't have
it. DragShift runs on native Wayland, so only the xcb comparison tests need it, and they skip
without it. To run them without installing anything:

```sh
mkdir -p /tmp/xcb && cd /tmp/xcb && apt-get download libxcb-cursor0 && dpkg-deb -x libxcb-cursor0_*.deb . && cd -
DRAGSHIFT_XCB_LIBS=/tmp/xcb/usr/lib/x86_64-linux-gnu uv run pytest -s tests/linux/test_display_backend.py
```

### LibreOffice inside the session

The snap LibreOffice can't start inside the session: `snap run` needs your real systemd
user manager for its scope (`… is not a snap cgroup for tag snap.libreoffice.libreoffice`),
so the app falls back to its built-in renderer there. Run the `libreoffice` tests on the
host (`uv run pytest -m libreoffice tests/test_soffice.py`), not in a session. A stray
snap LibreOffice ignores `kill` from a confined shell; stop it with
`systemctl --user stop snap.libreoffice.libreoffice-<uuid>.scope` (see `/proc/<pid>/cgroup`).

### Your desktop icons flickered

Something started GNOME Shell's DING without the launcher. DING kills every other DING of the
same user when it starts, and yours restarts. The launcher runs a private copy, so only
`python -m tests.linux.session`, the fixtures and the E2E runner may start a shell.

### `Wayland socket path too long; use a shorter /tmp`

A Unix socket path is at most 107 bytes. The launcher keeps `XDG_RUNTIME_DIR` short
(`/tmp/dsr.XXXX`) and checks it before booting. It always uses `/tmp`, not `$TMPDIR`; keep
it that way when changing the launcher.

### An app in the session has no display

Programs that D-Bus starts (Files, portals) get their environment from the bus, not from
the launcher. The launcher passes `WAYLAND_DISPLAY`, `DISPLAY` and `XAUTHORITY` with
`dbus-update-activation-environment`, so it is a required tool. Run your own programs with
`gnome_session.run`/`popen` or `env=gnome_session.env()`.

### The extension change doesn't show on your desktop

GNOME on Wayland loads a newly installed or changed extension only after you log out and
back in. Test changes in a session (`--extension DIR`) instead.

### The test helper on a real desktop

Never install `dragshift-test@eteen12.github.io` outside a session. It lets any program on
the session bus list and move windows and take screenshots. Only input injection and
screenshot files refuse to work outside a launcher session (headless shell, session root
`/tmp/dragshift-session-*`); the window methods have no such guard.

### `AccessDenied` from DragShift's extension

Every method but `Version` answers only the DragShift app (#93): the caller's uid must be
the shell's, `/proc/<pid>/exe` a Python interpreter and its command line `python -m
dragshift`, `python …/dragshift/__main__.py` or `~/.local/bin/dragshift`. `python run.py` is
refused. The shell log gets `DragShift: refused <method> to <bus name> (pid N): <reason>`.

In a launcher session only, pytest, `python -m tests.linux.<module>` and scripts in a
`tests/linux` directory are let in too. `python -c` and `gdbus` stay refused. The verdict is
cached per bus connection.

Trust model: the check keeps out casual and accidental callers (`gdbus`, scripts, other
apps), not code that already runs as you, which can start Python with DragShift's command
line anyway; `tests/linux/test_caller_check.py` shows this on purpose. Details are in
[LINUX-DEV.md](LINUX-DEV.md#who-may-call-the-extension-trust-model). So don't widen the
check to let a test in; move the test's code into a `tests/linux` module.

### A session test hangs at start-up or "helper not ready"

`start()` waits for the shell's `GNOME Shell started at` line (60 s) and fails with the log
tail. If a test changed the monitors before that, the overview stays up for good; change
them only after `start()`. Under heavy load (several sessions at once) a start can take
longer than usual; run fewer in parallel.
