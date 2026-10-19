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
    """One isolated GNOME Shell session shared by the whole test run."""
    require_session_tools()
    with Session() as s:
        yield s
