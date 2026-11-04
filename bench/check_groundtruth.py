#!/usr/bin/env python3
"""Check the benchmark's ground-truth files and print the true thread graph.

Usage: python3 bench/check_groundtruth.py [PREFIX]      (default: groundtruth)

Reads PREFIX.handoffs.csv and PREFIX.threads.csv written by ./pipeline.
Checks that the files are consistent, then prints the true edges: which
thread handed work to which thread, and how many times. The analyzer's
output is later scored against exactly this list.
"""
import csv
import sys
from collections import Counter

prefix = sys.argv[1] if len(sys.argv) > 1 else "groundtruth"

with open(f"{prefix}.threads.csv", newline="") as f:
    threads = {int(r["tid"]): r for r in csv.DictReader(f)}
with open(f"{prefix}.handoffs.csv", newline="") as f:
    rows = list(csv.DictReader(f))

errors = []


def name(tid):
    t = threads.get(tid)
    return f"{t['role']}[g{t['group']}.{t['index']}]({tid})" if t else f"unknown({tid})"


# Every item must be handed over exactly once per stage, in order.
by_item = {}
for r in rows:
    by_item.setdefault((r["group"], r["item_id"]), {}).setdefault(r["stage"], []).append(r)

for key, stages in sorted(by_item.items()):
    p2w = stages.get("producer_to_worker", [])
    w2s = stages.get("worker_to_sink", [])
    if len(p2w) != 1 or len(w2s) != 1:
        errors.append(f"group {key[0]} item {key[1]}: expected 1 handoff per stage, got {len(p2w)} and {len(w2s)}")
        continue
    a, b = p2w[0], w2s[0]
    if a["to_tid"] != b["from_tid"]:
        errors.append(f"group {key[0]} item {key[1]}: worker that received it is not the worker that sent it on")
    if int(a["put_ns"]) > int(a["get_ns"]) or int(b["put_ns"]) > int(b["get_ns"]):
        errors.append(f"group {key[0]} item {key[1]}: taken out before it was put in")
    if int(a["get_ns"]) > int(b["put_ns"]):
        errors.append(f"group {key[0]} item {key[1]}: forwarded before it was received")

# Every thread id must be in the thread table, with the roles we expect.
expected = {"producer_to_worker": ("producer", "worker"), "worker_to_sink": ("worker", "sink")}
for r in rows:
    f_t, t_t = threads.get(int(r["from_tid"])), threads.get(int(r["to_tid"]))
    if not f_t or not t_t:
        errors.append(f"unknown thread id in row {r}")
        continue
    if (f_t["role"], t_t["role"]) != expected[r["stage"]]:
        errors.append(f"unexpected roles {f_t['role']} -> {t_t['role']} in stage {r['stage']}")
    if f_t["group"] != t_t["group"]:
        errors.append(f"handoff crosses groups: {name(int(r['from_tid']))} -> {name(int(r['to_tid']))}")

edges = Counter((int(r["from_tid"]), int(r["to_tid"])) for r in rows)
print(f"{len(rows)} handoffs, {len(by_item)} items, {len(threads)} threads, {len(edges)} true edges")
for (a, b), n in sorted(edges.items(), key=lambda kv: (threads[kv[0][0]]["group"], kv[0])):
    print(f"  {name(a):28} -> {name(b):28} {n:6d} items")

if errors:
    print(f"\n{len(errors)} PROBLEM(S):")
    for e in errors[:20]:
        print("  -", e)
    sys.exit(1)
print("\nOK: ground truth is consistent")
