"""Throwaway #95 measurements in the isolated session (python -m from repo root via sys.path)."""
import json, subprocess, sys, time
from pathlib import Path
REPO = Path(sys.argv[1]); OUT = Path(sys.argv[2]); I = Path(__file__).parent
sys.path.insert(0, str(REPO))
from tests.linux.session import Session
from tests.linux.shell import ShellHelper
from tests.linux.files import Files

DESKTOP = "[Desktop Entry]\nType=Application\nName=DragShift\nExec=true\nIcon=dragshift\n"
PY = str(REPO / ".venv/bin/python")
res = {}

def m(s, method, sig="", *args):
    cmd = ["gdbus", "call", "--session", "--dest", "io.github.eteen12.Measure95", "--object-path",
           "/io/github/eteen12/Measure95", "--method", f"io.github.eteen12.Measure95.{method}", *args]
    r = s.run(cmd)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    # ('json',)
    return json.loads(eval(out)[0]) if out.startswith("('") else out

def mine(snap):
    return [w for w in snap["windows"] if (w["title"] or "").startswith("probe")]

with Session(helper=True, extensions=[I / "ext/measure95@eteen12.github.io"],
             data_files={"applications/io.github.eteen12.DragShift.desktop": DESKTOP}) as s, ShellHelper(s) as sh:
    sh.wait_ready()
    files = Files(s, sh)
    folder = files.make_folder("f", {"a.txt": b"x"})
    files.open_folder(folder)
    time.sleep(1.0)
    variants = ["menu-flags", "tooltip", "popup", "splash", "tool-with-parent",
                "modal-dialog-with-parent", "popup-with-parent"]
    for v in variants:
        files.open_folder(folder) if False else None
        fw = [w for w in sh.list_windows() if w["wm_class"] == "org.gnome.Nautilus"][0]
        sh.activate_window(fw["id"]); time.sleep(0.4)
        m(s, "Events")
        p = s.popen([PY, str(I / "qprobe.py"), v], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(2.5)
        snap = m(s, "Snapshot")
        res[v] = {"windows": mine(snap), "running": snap["running"], "tab": snap["tab"], "focus": snap["focus"],
                  "events": m(s, "Events")}
        if v == "menu-flags":
            res["foreign-dock"] = m(s, "Foreign", "", "probe menu-flags", "dock")
            res["foreign-hide"] = m(s, "Foreign", "", "probe menu-flags", "hide")
            snap = m(s, "Snapshot")
            res["foreign-after"] = {"windows": mine(snap), "running": snap["running"], "tab": snap["tab"]}
            sh.screenshot(s.root / "tmp" / "plain.png"); 
            import shutil; shutil.copy(s.root / "tmp" / "plain.png", OUT / "plain-menu-flags.png")
        p.terminate(); p.wait(5); time.sleep(0.5)
    # c': extension-spawned WaylandClient, several modes
    for mode in ["none", "hide-created", "dock-created", "dock-title", "dock-shown"]:
        fw = [w for w in sh.list_windows() if w["wm_class"] == "org.gnome.Nautilus"][0]
        sh.activate_window(fw["id"]); time.sleep(0.4)
        m(s, "Events")
        m(s, "Spawn", "", json.dumps([PY, str(I / "qprobe.py"), "menu-flags"]).replace('"', "'"), mode)
        time.sleep(3.0)
        snap = m(s, "Snapshot")
        res["spawn-" + mode] = {"windows": mine(snap), "running": snap["running"], "tab": snap["tab"],
                                "focus": snap["focus"], "events": m(s, "Events")}
        if mode == "dock-title":
            sh.screenshot(s.root / "tmp" / "dock.png")
            import shutil; shutil.copy(s.root / "tmp" / "dock.png", OUT / "spawn-dock-title.png")
        subprocess.run(["pkill", "-f", f"{I}/qprobe.py"])
        time.sleep(1.0)
    res["shell_pid"] = s.shell_pid
(OUT / "measure.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
