#!/usr/bin/env python3
"""Summarize a results CSV from run_traced.py.

Usage:
  python3 experiments/summarize_traced.py experiments/results/traced_<time>.csv

Prints, per configuration, over the VALID traced runs only:
  - precision / recall (directed and undirected) for the wakeup method and for the timing
    baseline, as mean [min-max], plus edges linking different groups (should be 0);
  - collector overhead: traced throughput and p95 latency against the paired untraced runs.
"n/a" means no run produced a value (for example no confident edges, so no precision).
Mean [min-max] over only a few runs is a rough guide, not a confidence interval.
"""
import argparse
import csv
import statistics as st
import sys
from collections import defaultdict


def nums(rows, key):
    out = []
    for r in rows:
        v = r.get(key, "")
        if v != "":
            out.append(float(v))
    return out


def span(vals, digits=3):
    if not vals:
        return "n/a"
    return f"{st.mean(vals):.{digits}f} [{min(vals):.{digits}f}-{max(vals):.{digits}f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    args = ap.parse_args()
    with open(args.csv, newline="") as f:
        rows = list(csv.DictReader(f))

    traced = defaultdict(list)
    untraced = defaultdict(list)
    skipped = 0
    for r in rows:
        if r["valid"] != "yes":
            skipped += 1
            continue
        (traced if r["traced"] == "yes" else untraced)[r["config"]].append(r)

    print("Accuracy (valid traced runs)")
    for name, rs in traced.items():
        print(f"\n{name}  ({len(rs)} runs)")
        for method in ("wakeup", "timing"):
            p = f"{method}_"
            print(f"  {method:7} directed   precision {span(nums(rs, p + 'dir_precision'))}  recall {span(nums(rs, p + 'dir_recall'))}")
            print(f"  {'':7} undirected precision {span(nums(rs, p + 'undir_precision'))}  recall {span(nums(rs, p + 'undir_recall'))}")
            cg = nums(rs, p + "cross_group")
            print(f"  {'':7} edges between groups: mean {st.mean(cg):.1f}, max {max(cg):.0f}" if cg else f"  {'':7} edges between groups: n/a")

    print("\nCollector overhead (traced vs paired untraced runs; negative = traced was lower)")
    print(f"{'config':15} {'runs':>9} {'throughput':>12} {'p95 latency':>12}")
    for name, rs in traced.items():
        us = untraced.get(name, [])
        if not us:
            continue
        tt, ut = nums(rs, "throughput"), nums(us, "throughput")
        tp, up = nums(rs, "p95_ms"), nums(us, "p95_ms")
        thr = f"{(st.mean(tt) / st.mean(ut) - 1) * 100:+.1f}%" if tt and ut else "n/a"
        p95 = f"{(st.mean(tp) / st.mean(up) - 1) * 100:+.1f}%" if tp and up and st.mean(up) else "n/a"
        print(f"{name:15} {len(rs):>4}/{len(us):<4} {thr:>12} {p95:>12}")
    if skipped:
        print(f"\n{skipped} invalid run(s) excluded (valid != yes); see the note column in the CSV.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
