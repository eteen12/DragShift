"""Fixtures for tests that run inside the isolated headless GNOME Shell session."""
import os

import pytest

from tests.linux.session import Session, missing_tools

# The real desktop's display variables as seen before any session started.
REAL_ENV = {k: os.environ.get(k) for k in
            ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}


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
