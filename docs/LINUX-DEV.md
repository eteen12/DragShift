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

## Isolated GNOME Shell session (`-m gnome` tests)

Tests marked `gnome` run inside a throwaway headless GNOME Shell 46 session started by
`tests/linux/session.py`. It needs `gnome-shell`, `dbus` (`dbus-run-session`,
`dbus-daemon`, `dbus-update-activation-environment`), `gdbus` and `python3-gi`, which a
normal Ubuntu desktop already has. Without them, the `gnome` tests are skipped.

```sh
uv run pytest -m gnome tests/linux          # session tests only
uv run python -m tests.linux.session -- nautilus --new-window /tmp   # run a command inside
uv run python -m tests.linux.session        # print the session env, Ctrl-C to stop
```

In tests, use the session-scoped `gnome_session` fixture. `gnome_session.env()` is the
environment for child processes, `gnome_session.run(cmd)` and `gnome_session.popen(cmd)`
run inside the session, and `gnome_session.log_path` is the shell's log.
`Session(extensions=[dir])` installs and enables extra extensions.

The session never touches your real desktop:
- It uses its own HOME, XDG directories, `XDG_RUNTIME_DIR` (`/tmp/dsr.*`), session bus
  and an empty private system bus.
- It runs a private copy of DING, because DING kills every other DING of the same user
  when it starts.
- Teardown kills every process that carries the session's runtime dir, then deletes the
  temporary directories. This also happens on Ctrl-C. A session whose launcher was
  SIGKILLed is cleaned up by the next start.
