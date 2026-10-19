# TEMPORARY (#85): apply one experiment to tests/test_result_actions.py in the CI checkout.
import sys
from pathlib import Path
p = Path("tests/test_result_actions.py")
s = p.read_text(encoding="utf-8")
exp = sys.argv[1]
patch = '    monkeypatch.setattr(toasts.sys, "platform", platform)\n'
assert patch in s
LOG = 'print("[diag] gc", file=sys.__stderr__, flush=True); '
DONE = '; print("[diag] gc done", file=sys.__stderr__, flush=True)'
if exp == "gc_patched_noworker":
    s = s.replace(patch, patch + "    from PySide6.QtCore import QThreadPool; QThreadPool.globalInstance().waitForDone(); "
                  "import gc; " + LOG + "gc.collect()" + DONE + "\n", 1)
elif exp == "gc_patched_worker":
    s = s.replace(patch, patch + "    import gc; " + LOG + "gc.collect()" + DONE + "\n", 1)
elif exp == "gc_genuine_worker":
    s = s.replace(patch, "    import gc; " + LOG + "gc.collect()" + DONE + "\n" + patch, 1)
elif exp in ("collectable", "gc_genuine_noworker"):
    dbg = "gc.set_debug(gc.DEBUG_COLLECTABLE); " if exp == "collectable" else ""
    s = s.replace(patch, "    from PySide6.QtCore import QThreadPool; QThreadPool.globalInstance().waitForDone(); "
                  "import gc; " + LOG + dbg + "gc.collect()" + DONE + "\n" + patch, 1)
elif exp == "garbage":
    s = s.replace(patch, "    from PySide6.QtCore import QThreadPool; QThreadPool.globalInstance().waitForDone(); "
                  "import gc, collections; gc.set_debug(gc.DEBUG_SAVEALL); gc.collect(); gc.set_debug(0); "
                  "print('[diag] garbage', collections.Counter(type(o).__module__ + '.' + type(o).__qualname__ "
                  "for o in gc.garbage).most_common(60), file=sys.__stderr__, flush=True); gc.garbage.clear()\n" + patch, 1)
else:
    raise SystemExit("unknown " + exp)
p.write_text(s, encoding="utf-8")
print("applied", exp)
