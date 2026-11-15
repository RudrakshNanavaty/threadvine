#!/usr/bin/env python3
"""Check the benchmark's ground-truth files and print the true thread graph.

Usage:
  python3 bench/check_groundtruth.py [PREFIX] [--items N] [--groups G] [--workers W] [--quiet]

PREFIX defaults to "groundtruth". It reads PREFIX.handoffs.csv and
PREFIX.threads.csv written by ./build/pipeline.

Without options it checks that the files are consistent with each other.
It can only notice a missing item if a later item exists, because it does not
know how many items the run was supposed to have. Pass the settings you used
for the run to make the check complete:

  --items N     items per group (the -n option); catches items missing from the end
                (checked on the timed items; with -W the profile-phase items are
                checked only for gaps, because how many there are depends on the time)
  --groups G    number of groups (the -g option)
  --workers W   workers per group (the -w option)

Exit status: 0 = consistent, 1 = at least one problem, 2 = files unreadable.
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict

ROLES = {"main", "producer", "worker", "sink"}
STAGES = {"producer_to_worker": ("producer", "worker"), "worker_to_sink": ("worker", "sink")}


def load(prefix):
    with open(f"{prefix}.threads.csv", newline="") as f:
        threads_rows = list(csv.DictReader(f))
    with open(f"{prefix}.handoffs.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    return threads_rows, rows


def describe(threads, tid):
    t = threads.get(tid)
    return f"{t['role']}[g{t['group']}.{t['index']}]({tid})" if t else f"unknown({tid})"


def _missing(present, expected_count=None):
    """IDs missing from `present` (a set of ints): from 0..expected_count-1 when the
    count is known, otherwise from 0..max(present)."""
    top = expected_count if expected_count is not None else (max(present) + 1 if present else 0)
    return [i for i in range(top) if i not in present]


def _fmt(ids, limit=8):
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "")


def check(prefix, items=None, groups=None, workers=None):
    """Return a dict: errors (list), notes (list), edges (Counter of (from, to)),
    threads (dict tid -> row), n_rows, n_items."""
    errors, notes = [], []
    threads_rows, rows = load(prefix)

    # ---- thread table ----
    threads = {}
    for r in threads_rows:
        tid = int(r["tid"])
        if tid in threads:
            errors.append(f"thread id {tid} appears twice in the thread table")
        threads[tid] = {"role": r["role"], "group": int(r["group"]), "index": int(r["index"])}
        if r["role"] not in ROLES:
            errors.append(f"thread {tid} has unknown role '{r['role']}'")

    if sum(1 for t in threads.values() if t["role"] == "main") != 1:
        errors.append("expected exactly one main thread in the thread table")

    by_group = defaultdict(lambda: defaultdict(list))
    for tid, t in threads.items():
        if t["role"] != "main":
            by_group[t["group"]][t["role"]].append(t["index"])

    found_groups = set(by_group)
    expected_groups = set(range(groups)) if groups is not None else set(range(max(found_groups) + 1)) if found_groups else set()
    for g in sorted(expected_groups - found_groups):
        errors.append(f"group {g} has no threads in the thread table")
    for g in sorted(found_groups - expected_groups):
        errors.append(f"group {g} is in the thread table but was not expected")
    for g in sorted(found_groups & expected_groups):
        roles = by_group[g]
        for role in ("producer", "sink"):
            if len(roles[role]) != 1:
                errors.append(f"group {g}: expected 1 {role}, found {len(roles[role])}")
        n_workers = len(roles["worker"])
        if workers is not None and n_workers != workers:
            errors.append(f"group {g}: expected {workers} workers, found {n_workers}")
        if n_workers < 1:
            errors.append(f"group {g}: no workers")
        if sorted(roles["worker"]) != list(range(n_workers)):
            errors.append(f"group {g}: worker indexes are not 0..{n_workers - 1}")

    # ---- handoff rows ----
    parsed = []
    for r in rows:
        try:
            parsed.append({
                "group": int(r["group"]), "item": int(r["item_id"]),
                "from": int(r["from_tid"]), "to": int(r["to_tid"]), "stage": r["stage"],
                "put": int(r["put_ns"]), "get": int(r["get_ns"]),
                "phase": r.get("phase") or "timed",  # files written before -W existed have no column
            })
            if parsed[-1]["phase"] not in ("profile", "timed"):
                errors.append(f"unknown phase '{parsed[-1]['phase']}' in group {r['group']} item {r['item_id']}")
        except (ValueError, KeyError) as e:
            errors.append(f"unreadable row {r}: {e}")

    by_item = defaultdict(lambda: defaultdict(list))
    for r in parsed:
        by_item[(r["group"], r["phase"], r["item"])][r["stage"]].append(r)
        if r["stage"] not in STAGES:
            errors.append(f"unknown stage '{r['stage']}' in group {r['group']} item {r['item']}")
            continue
        f_t, t_t = threads.get(r["from"]), threads.get(r["to"])
        if not f_t or not t_t:
            errors.append(f"unknown thread id in group {r['group']} item {r['item']} ({r['stage']})")
            continue
        if (f_t["role"], t_t["role"]) != STAGES[r["stage"]]:
            errors.append(f"unexpected roles {f_t['role']} -> {t_t['role']} in stage {r['stage']}")
        if f_t["group"] != t_t["group"] or f_t["group"] != r["group"]:
            errors.append(f"handoff crosses groups: {describe(threads, r['from'])} -> {describe(threads, r['to'])}")
        if r["put"] > r["get"]:
            errors.append(f"group {r['group']} item {r['item']}: taken out before it was put in ({r['stage']})")

    for (g, phase, item), stages in sorted(by_item.items()):
        label = f"item {item}" if phase == "timed" else f"profile item {item}"
        p2w, w2s = stages.get("producer_to_worker", []), stages.get("worker_to_sink", [])
        if len(p2w) != 1 or len(w2s) != 1:
            errors.append(f"group {g} {label}: expected 1 handoff per stage, got {len(p2w)} and {len(w2s)}")
            continue
        a, b = p2w[0], w2s[0]
        if a["to"] != b["from"]:
            errors.append(f"group {g} {label}: the worker that received it is not the worker that sent it on")
        if a["get"] > b["put"]:
            errors.append(f"group {g} {label}: forwarded before it was received")

    # ---- are all expected items present? (the case the old check could not see) ----
    items_by_group = defaultdict(set)      # timed items
    profile_by_group = defaultdict(set)    # profile-phase items (only with -W)
    for (g, phase, item) in by_item:
        (items_by_group if phase == "timed" else profile_by_group)[g].add(item)
    for g in sorted(expected_groups):
        missing = _missing(items_by_group.get(g, set()), items)
        if missing:
            errors.append(f"group {g}: items missing from the answer key: {_fmt(missing)}")
        extra = [i for i in items_by_group.get(g, set()) if items is not None and i >= items]
        if extra:
            errors.append(f"group {g}: unexpected item ids {_fmt(sorted(extra))}")
        gaps = _missing(profile_by_group.get(g, set()))
        if gaps:
            errors.append(f"group {g}: profile-phase items missing from the answer key: {_fmt(gaps)}")
    if profile_by_group:
        notes.append("profile-phase items per group: " +
                     ", ".join(f"g{g}={len(v)}" for g, v in sorted(profile_by_group.items())))
    if items is None:
        notes.append("--items was not given, so an item missing from the END of a group cannot be detected")

    edges = Counter((r["from"], r["to"]) for r in parsed if r["stage"] in STAGES)
    return {"errors": errors, "notes": notes, "edges": edges, "threads": threads,
            "n_rows": len(parsed), "n_items": len(by_item)}


def main():
    ap = argparse.ArgumentParser(description="Check the benchmark's ground-truth files.")
    ap.add_argument("prefix", nargs="?", default="groundtruth")
    ap.add_argument("--items", type=int, help="items per group (the -n option)")
    ap.add_argument("--groups", type=int, help="number of groups (the -g option)")
    ap.add_argument("--workers", type=int, help="workers per group (the -w option)")
    ap.add_argument("--quiet", action="store_true", help="print only the verdict")
    args = ap.parse_args()

    try:
        res = check(args.prefix, args.items, args.groups, args.workers)
    except (OSError, KeyError, csv.Error) as e:
        print(f"cannot read ground-truth files for prefix '{args.prefix}': {e}", file=sys.stderr)
        return 2

    errors, threads, edges = res["errors"], res["threads"], res["edges"]
    if not args.quiet:
        print(f"{res['n_rows']} handoffs, {res['n_items']} items, {len(threads)} threads, {len(edges)} true edges")
        for (a, b), n in sorted(edges.items(), key=lambda kv: (threads.get(kv[0][0], {"group": -2})["group"], kv[0])):
            print(f"  {describe(threads, a):28} -> {describe(threads, b):28} {n:6d} items")
        for note in res["notes"]:
            print(f"note: {note}")
        print()
    if errors:
        print(f"FAIL: {len(errors)} problem(s)")
        for e in errors[:20]:
            print("  -", e)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        return 1
    print("OK: ground truth is consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
