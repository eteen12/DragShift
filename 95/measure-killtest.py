import json, subprocess, sys, time
from pathlib import Path
REPO = Path(sys.argv[1]); I = Path(__file__).parent
sys.path.insert(0, str(REPO))
from tests.linux.session import Session
PY = str(REPO / ".venv/bin/python")
def call(s, method, *args):
    return s.run(["gdbus", "call", "--session", "--dest", "io.github.eteen12.Measure95", "--object-path",
           "/io/github/eteen12/Measure95", "--method", f"io.github.eteen12.Measure95.{method}", *args])
with Session(extensions=[I / "ext/measure95@eteen12.github.io"]) as s:
    for _ in range(50):
        if call(s, "Events").returncode == 0: break
        time.sleep(0.2)
    print(call(s, "Spawn", f"['{PY}', '{I}/qprobe.py', 'menu-flags']", "none").stdout)
    time.sleep(3)
    pids = subprocess.run(["pgrep", "-f", f"{I}/qprobe.py"], capture_output=True, text=True).stdout.split()
    print("probe pids", pids, "shell", s.shell_pid)
    print("kill:", call(s, "Kill", "probe menu-flags"))
    time.sleep(2)
    alive = Path(f"/proc/{s.shell_pid}").exists()
    print("shell alive after kill:", alive, call(s, "Events").returncode)
    print("probe alive:", [p for p in pids if Path(f"/proc/{p}").exists()])
    print(s.log_tail(15))
