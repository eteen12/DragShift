"""Stress the isolated-session launcher: N rounds of P parallel `Session(helper=True)` starts.

    python stress.py REPO OUTDIR --rounds 50 --parallel 1
Each start runs in its own process (like separate pytest runs). Failures keep the full
shell log in OUTDIR; every start's helper trace lines and timings go to OUTDIR/results.jsonl.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def one(repo, out, tag, scale):
    sys.path.insert(0, repo)
    os.chdir(repo)
    from tests.linux import session as sm
    t0 = time.monotonic()
    s = sm.Session(helper=True, **({"size": (3200, 1800), "scale": 2} if scale == 2 else {}))
    orig_stop = s.stop
    saved = {}

    def stop():
        if s.log_path and s.log_path.exists() and "log" not in saved:
            saved["log"] = s.log_path.read_text(errors="replace")
        orig_stop()
    s.stop = stop
    res = {"tag": tag, "scale": scale}
    try:
        s.start()
        res["ok"] = True
        res["t"] = round(time.monotonic() - t0, 2)
        from tests.linux.shell import ShellHelper
        with ShellHelper(s) as h:
            res["version"] = h.version()
    except BaseException as e:  # noqa
        res["ok"] = False
        res["t"] = round(time.monotonic() - t0, 2)
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        s.stop()
    log = saved.get("log", "")
    res["trace"] = [ln for ln in log.splitlines() if "dragshift-test@" in ln][-12:]
    if not res["ok"]:
        (Path(out) / f"fail-{tag}.log").write_text(log)
    print(json.dumps(res), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("out")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--one")
    ap.add_argument("--scale", type=float, default=1)
    ap.add_argument("--every", type=int, default=4, help="every Nth start uses scale 2")
    ap.add_argument("--load", type=int, default=0, help="CPU burner processes during the run")
    a = ap.parse_args()
    if a.one:
        return one(a.repo, a.out, a.one, a.scale)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    ok = fail = 0
    burners = [subprocess.Popen([sys.executable, "-c", "import time\nt = time.time() + 900\nwhile time.time() < t: pass"])
               for _ in range(a.load)]
    with open(out / "results.jsonl", "a") as rf:
        for r in range(a.rounds):
            procs = []
            for p in range(a.parallel):
                scale = 2 if (r * a.parallel + p) % a.every == a.every - 1 else 1
                procs.append(subprocess.Popen(
                    [py, __file__, a.repo, a.out, "--one", f"r{r}p{p}", "--scale", str(scale)],
                    stdout=subprocess.PIPE, text=True))
            for p in procs:
                line = (p.communicate()[0].strip().splitlines() or ['{"ok": false, "error": "no output"}'])[-1]
                rf.write(line + "\n")
                rf.flush()
                d = json.loads(line)
                ok += d["ok"]
                fail += not d["ok"]
                print(f"{d.get('tag')} ok={d['ok']} t={d.get('t')} {d.get('error', '')[:160]}",
                      flush=True)
    for b in burners:
        b.kill()
        b.wait()
    print(f"TOTAL ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
