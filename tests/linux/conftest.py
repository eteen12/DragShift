"""Fixtures for tests that run inside the isolated headless GNOME Shell session.

Everything under tests/linux/ is Linux-only (it reads /proc, uses os.getuid and GNOME
Shell), so nothing here is collected on Windows or macOS.
"""
import os
import sys

import pytest

if not sys.platform.startswith("linux"):
    collect_ignore_glob = ["*"]
else:
    from tests.linux.session import Session, missing_tools
    from tests.linux.shell import ShellHelper

# The real desktop's display variables as seen before any session started.
REAL_ENV = {k: os.environ.get(k) for k in
            ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
             "TMPDIR")}


def require_session_tools():
    missing = missing_tools()
    if missing:
        pytest.skip("isolated GNOME session unavailable, missing: " + ", ".join(missing))


@pytest.fixture(scope="session")
def gnome_session():
    """One isolated GNOME Shell session shared by the whole test run, with the test-helper
    extension installed (use the `shell_helper` fixture to call it)."""
    require_session_tools()
    with Session(helper=True) as s:
        yield s


@pytest.fixture(scope="session")
def shell_helper(gnome_session):
    """A ready ShellHelper for the shared session (screenshots, window list, pointer).
    Fails with the shell log tail if the helper extension did not load."""
    with ShellHelper(gnome_session) as helper:
        helper.wait_ready()
        yield helper
