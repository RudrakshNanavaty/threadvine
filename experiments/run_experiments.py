#!/usr/bin/env python3
"""Run named benchmark configurations several times and write one results CSV.

Usage (from the repo root, after `make bench`):
  python3 experiments/run_experiments.py --list
  python3 experiments/run_experiments.py --configs paced_sleep,paced_spin --repeats 3
  python3 experiments/run_experiments.py --configs all --repeats 5 --out experiments/results/run1.csv

Each run starts ./build/pipeline, parses its summary lines, then validates the
ground-truth files with bench/check_groundtruth.py using the settings of that run.
A run only counts as valid if the program exited 0 AND the validator said OK.
The ground-truth files are deleted after validation unless --keep is given.
Output goes to experiments/results/ (git-ignored). Run configs in an interleaved
order (round 1 of every config, then round 2, ...) so slow drift on the machine
affects all configs alike.
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
import check_groundtruth as cg  # noqa: E402

# name -> pipeline options. Keep the sets small and each name self-explanatory.
CONFIGS = {
    "basic":          dict(w=1, g=1, n=50,  p=10000, c=5000, m="sleep", q=8),
    "workers4":       dict(w=4, g=1, n=100, p=10000, c=5000, m="sleep", q=8),
    "groups2":        dict(w=2, g=2, n=100, p=10000, c=5000, m="sleep", q=8),
    "paced_sleep":    dict(w=4, g=1, n=200, p=5000,  c=5000, m="sleep", q=8),
    "paced_spin":     dict(w=4, g=1, n=200, p=5000,  c=5000, m="spin",  q=8),
    "unpaced_sleep":  dict(w=4, g=1, n=400, p=0,     c=5000, m="sleep", q=8),
    "unpaced_spin":   dict(w=4, g=1, n=400, p=0,     c=5000, m="spin",  q=8),
    "q1_sleep":       dict(w=4, g=1, n=400, p=0,     c=5000, m="sleep", q=1),
    "q1_spin":        dict(w=4, g=1, n=400, p=0,     c=5000, m="spin",  q=1),
}
OPT_COLUMNS = ["w", "g", "n", "p", "c", "m", "q"]
COLUMNS = (["config", "repeat", "timestamp"] + OPT_COLUMNS +
           ["elapsed_s", "throughput", "p50_ms", "p95_ms", "p99_ms", "max_ms",
            "handoffs", "exit_code", "validation", "valid", "note"])

NUM = r"([0-9.]+)"
RE_ELAPSED = re.compile(r"elapsed_s=" + NUM + r"\s+throughput=" + NUM)
RE_LAT = re.compile(r"p50=" + NUM + r"\s+p95=" + NUM + r"\s+p99=" + NUM + r"\s+max=" + NUM)
RE_HAND = re.compile(r"handoffs=(\d+)")


def parse_summary(text):
    out = {}
    m = RE_ELAPSED.search(text)
    if m:
        out["elapsed_s"], out["throughput"] = m.groups()
    m = RE_LAT.search(text)
    if m:
        out["p50_ms"], out["p95_ms"], out["p99_ms"], out["max_ms"] = m.groups()
    m = RE_HAND.search(text)
    if m:
        out["handoffs"] = m.group(1)
    return out


def run_once(binary, name, opts, repeat, keep, timeout):
    row = {"config": name, "repeat": repeat, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
           **{k: opts[k] for k in OPT_COLUMNS}, "note": ""}
    tmp = tempfile.mkdtemp(prefix="exp_")
    prefix = os.path.join(tmp, "gt")
    cmd = [binary] + [x for k in OPT_COLUMNS for x in (f"-{k}", str(opts[k]))] + ["-o", prefix]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        row.update(exit_code="timeout", validation="not run", valid="no", note=f"timed out after {timeout}s")
        return row
    row["exit_code"] = proc.returncode
    row.update(parse_summary(proc.stdout))
    if proc.returncode != 0:
        row.update(validation="not run", valid="no", note=(proc.stderr.strip().splitlines() or ["nonzero exit"])[-1])
    else:
        try:
            res = cg.check(prefix, items=opts["n"], groups=opts["g"], workers=opts["w"])
        except Exception as e:  # unreadable files etc.
            res = {"errors": [f"validator crashed: {e}"]}
        if res["errors"]:
            row.update(validation="FAIL", valid="no", note=res["errors"][0])
        else:
            row.update(validation="OK", valid="yes")
    if keep:
        row["note"] = (row["note"] + f" kept in {tmp}").strip()
    else:
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list configurations and exit")
    ap.add_argument("--configs", default="basic", help="comma-separated names, or 'all'")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--binary", default=os.path.join(ROOT, "build", "pipeline"))
    ap.add_argument("--out", default=None, help="results CSV (default experiments/results/results_<time>.csv)")
    ap.add_argument("--timeout", type=int, default=120, help="seconds allowed per run")
    ap.add_argument("--keep", action="store_true", help="keep ground-truth files of every run")
    args = ap.parse_args()

    if args.list:
        for name, o in CONFIGS.items():
            print(f"{name:15} " + " ".join(f"-{k} {o[k]}" for k in OPT_COLUMNS))
        return 0
    names = list(CONFIGS) if args.configs == "all" else [s.strip() for s in args.configs.split(",") if s.strip()]
    unknown = [n for n in names if n not in CONFIGS]
    if unknown or args.repeats < 1:
        print(f"error: unknown config(s) {unknown} or bad --repeats; use --list", file=sys.stderr)
        return 2
    if not os.path.exists(args.binary):
        print(f"error: {args.binary} not found; run `make bench` first", file=sys.stderr)
        return 2

    out = args.out or os.path.join(HERE, "results", time.strftime("results_%Y%m%d_%H%M%S.csv"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    bad = 0
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for rep in range(1, args.repeats + 1):          # interleaved: all configs per round
            for name in names:
                row = run_once(args.binary, name, CONFIGS[name], rep, args.keep, args.timeout)
                w.writerow(row)
                f.flush()
                bad += row["valid"] != "yes"
                print(f"[{rep}/{args.repeats}] {name:15} valid={row['valid']:3} "
                      f"throughput={row.get('throughput', '-')} p95={row.get('p95_ms', '-')}", flush=True)
    print(f"wrote {out}  ({bad} invalid run(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
