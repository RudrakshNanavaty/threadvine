#!/usr/bin/env python3
"""Summarize a results CSV from run_experiments.py: one line per config.

Usage:
  python3 experiments/summarize_results.py experiments/results/run1.csv
  python3 experiments/summarize_results.py run1.csv --compare paced_sleep paced_spin

Only rows with valid=yes are used. Reports runs, mean, min, max and the
spread (max-min as % of mean) for throughput and p95 latency. With only a few
runs treat differences smaller than the spread as noise.
--compare A B prints B relative to A (positive = B higher).
"""
import argparse
import csv
import statistics as st
import sys
from collections import defaultdict


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def stats(vals):
    m = st.mean(vals)
    spread = (max(vals) - min(vals)) / m * 100 if m else 0.0
    return m, min(vals), max(vals), spread


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    rows = load(args.csv)
    good = [r for r in rows if r["valid"] == "yes"]
    skipped = len(rows) - len(good)
    by = defaultdict(list)
    for r in good:
        by[r["config"]].append(r)

    print(f"{'config':15} {'runs':>4} {'thr mean':>9} {'min':>8} {'max':>8} {'spread%':>8} {'p95 mean ms':>12} {'spread%':>8}")
    summary = {}
    for name, rs in by.items():
        t = stats([float(r["throughput"]) for r in rs])
        p = stats([float(r["p95_ms"]) for r in rs])
        summary[name] = (t, p, len(rs))
        print(f"{name:15} {len(rs):4d} {t[0]:9.1f} {t[1]:8.1f} {t[2]:8.1f} {t[3]:8.1f} {p[0]:12.2f} {p[3]:8.1f}")
    if skipped:
        print(f"\n{skipped} invalid run(s) excluded (valid != yes).")

    if args.compare:
        a, b = args.compare
        if a not in summary or b not in summary:
            print(f"error: need valid runs for both {a} and {b}", file=sys.stderr)
            return 2
        (ta, pa, na), (tb, pb, nb) = summary[a], summary[b]
        d_t = (tb[0] - ta[0]) / ta[0] * 100
        d_p = (pb[0] - pa[0]) / pa[0] * 100 if pa[0] else float("nan")
        print(f"\n{b} vs {a}: throughput {d_t:+.1f}%, p95 latency {d_p:+.1f}%  (runs {nb} vs {na})")
        noisy = max(ta[3], tb[3])
        if abs(d_t) < noisy:
            print(f"note: throughput difference is smaller than the run-to-run spread ({noisy:.1f}%), so it may be noise")
        if na != nb:
            print("note: unequal number of runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
