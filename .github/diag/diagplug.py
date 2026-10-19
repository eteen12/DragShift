# TEMPORARY (#85) diagnostics, loaded with -p diagplug.
import gc
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _diag(request):
    if os.environ.get("DIAG_GC"):
        print(f"\n[diag] gc before {request.node.nodeid}", file=sys.__stderr__, flush=True)
        gc.collect()
        print(f"[diag] gc before done {request.node.nodeid}", file=sys.__stderr__, flush=True)
    yield
    if os.environ.get("DIAG_GC"):
        print(f"[diag] gc after {request.node.nodeid}", file=sys.__stderr__, flush=True)
        gc.collect()
        print(f"[diag] gc after done {request.node.nodeid}", file=sys.__stderr__, flush=True)
