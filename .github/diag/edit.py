# TEMPORARY (#85): apply one experiment to tests/test_result_actions.py in the CI checkout.
import sys
from pathlib import Path
p = Path("tests/test_result_actions.py")
s = p.read_text(encoding="utf-8")
exp = sys.argv[1]
if exp == "sysmod":
    old = 'monkeypatch.setattr(toasts.sys, "platform", platform)'
    assert s.count(old) == 2
    s = s.replace(old, 'monkeypatch.setattr(toasts, "sys", __import__("types").SimpleNamespace(platform=platform))')
elif exp == "wait":
    old = "    c = toasts.ToastStack().new()\n    yield c\n"
    assert old in s
    s = s.replace(old, "    c = toasts.ToastStack().new()\n"
                       "    from PySide6.QtCore import QThreadPool\n"
                       "    QThreadPool.globalInstance().waitForDone()\n"
                       "    qapp.processEvents()\n"
                       "    yield c\n")
p.write_text(s, encoding="utf-8")
print("applied", exp)
