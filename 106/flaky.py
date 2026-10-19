"""Run one pytest node id ROUNDS times, PARALLEL copies at a time; report pass counts.

    python flaky.py REPO OUTDIR ROUNDS PARALLEL NODEID
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

repo, out, rounds, par, node = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
out.mkdir(parents=True, exist_ok=True)
py = str(Path(repo) / ".venv/bin/python")
ok = fail = 0
for r in range(rounds):
    procs = []
    for p in range(par):
        bt = out / f"bt-r{r}p{p}"
        log = open(out / f"r{r}p{p}.log", "w")
        procs.append((subprocess.Popen(
            [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={bt}", node],
            cwd=repo, stdout=log, stderr=subprocess.STDOUT), log, bt, f"r{r}p{p}"))
    for proc, log, bt, tag in procs:
        rc = proc.wait()
        log.close()
        shutil.rmtree(bt, ignore_errors=True)
        last = (out / f"{tag}.log").read_text().strip().splitlines()[-1:]
        good = rc == 0 and last and " passed" in last[0] and "failed" not in last[0] \
            and "error" not in last[0]
        ok += good
        fail += not good
        print(f"{tag} rc={rc} {last[0] if last else ''}", flush=True)
print(f"TOTAL {node}: ok={ok} fail={fail}", flush=True)
