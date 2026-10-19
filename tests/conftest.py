"""Test-session defaults so the suite runs anywhere and never touches the real desktop.

- Puts the repo root on sys.path, so a bare `pytest` works as well as `python -m pytest`.
- Uses Qt's offscreen platform unless the caller chose one (no DISPLAY/WAYLAND_DISPLAY needed).
- Points the XDG base directories at a throwaway folder, so QSettings, logs and caches go
  there instead of ~/.config/DragShift, ~/.local/state, ~/.local/share or ~/.cache.
- Hides an installed LibreOffice from DOCX -> PDF, so those tests cover the built-in
  renderer; only tests/test_soffice.py uses a fake or (marked `libreoffice`) the real one.

This runs when pytest loads the conftest, before any test module (or Qt) is imported.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_XDG_ROOT = Path(tempfile.mkdtemp(prefix="dragshift-tests-"))
for _var, _sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                   ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
    (_XDG_ROOT / _sub).mkdir()
    os.environ[_var] = str(_XDG_ROOT / _sub)


def pytest_configure(config):
    config.addinivalue_line("markers", "libreoffice: runs the installed LibreOffice (skipped when missing)")


@pytest.fixture(autouse=True)
def _no_real_libreoffice(request, monkeypatch):
    if not sys.platform.startswith("linux") or request.module.__name__.endswith("test_soffice"):
        return
    from dragshift import platform_linux
    monkeypatch.setattr(platform_linux, "soffice_binary", lambda: None)


def pytest_unconfigure(config):
    shutil.rmtree(_XDG_ROOT, ignore_errors=True)
