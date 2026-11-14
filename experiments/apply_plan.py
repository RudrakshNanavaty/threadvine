#!/usr/bin/env python3
"""Pin the threads of a running process to CPUs.

Strategies:
  default      leave every thread alone (no pinning)
  round-robin  sorted thread ids, thread i -> CPU (i mod cpu_count)
  plan         read a plan file written by `analyzer plan`:
               {"cpu_count": 4, "assignments": {"<tid>": [0, 1]}, "unpinned": [...]}

Examples:
  python3 experiments/apply_plan.py 12345 --strategy round-robin
  python3 experiments/apply_plan.py 12345 --strategy plan --plan plan.json
  python3 experiments/apply_plan.py 12345 --strategy plan --plan plan.json --dry-run

Exit status: 0 all requested pins worked, 1 some failed, 2 bad input.
"""
import argparse
import json
import os
import sys


def list_tids(pid):
    """Thread ids of a process, sorted. Raises FileNotFoundError if it is gone."""
    return sorted(int(t) for t in os.listdir(f"/proc/{pid}/task"))


def round_robin_assignments(tids, cpu_count):
    return {tid: [i % cpu_count] for i, tid in enumerate(tids)}


def load_plan(path):
    """Return (cpu_count, {tid: [cpus]}, set(unpinned tids)). Raises ValueError if malformed."""
    with open(path) as f:
        plan = json.load(f)
    if not isinstance(plan, dict) or "assignments" not in plan:
        raise ValueError("plan file needs an 'assignments' object")
    cpu_count = plan.get("cpu_count")
    assignments = {}
    for tid, cpus in plan["assignments"].items():
        if not isinstance(cpus, list) or not cpus or not all(isinstance(c, int) and c >= 0 for c in cpus):
            raise ValueError(f"assignment for thread {tid} must be a non-empty list of CPU numbers")
        assignments[int(tid)] = cpus
    unpinned = {int(t) for t in plan.get("unpinned", [])}
    return cpu_count, assignments, unpinned


def apply(pid, strategy, plan_path=None, cpu_count=None, dry_run=False):
    """Apply a strategy. Returns a dict describing what happened."""
    tids = list_tids(pid)
    result = {"pid": pid, "strategy": strategy, "pinned": {}, "failed": {},
              "left_alone": [], "missing_from_plan": [], "plan_tids_not_found": []}

    if strategy == "default":
        result["left_alone"] = tids
        return result

    if strategy == "round-robin":
        cpus_n = cpu_count or os.cpu_count()
        assignments = round_robin_assignments(tids, cpus_n)
        unpinned = set()
    elif strategy == "plan":
        if not plan_path:
            raise ValueError("--plan is required with --strategy plan")
        plan_cpus, assignments, unpinned = load_plan(plan_path)
        cpus_n = cpu_count or plan_cpus or os.cpu_count()
    else:
        raise ValueError(f"unknown strategy {strategy!r}")

    online = set(range(os.cpu_count()))
    for cpus in assignments.values():
        bad = [c for c in cpus if c not in online]
        if bad:
            raise ValueError(f"plan uses CPUs {bad} but this machine has CPUs 0-{os.cpu_count() - 1}")

    tidset = set(tids)
    result["plan_tids_not_found"] = sorted(t for t in assignments if t not in tidset)
    result["missing_from_plan"] = sorted(t for t in tids if t not in assignments and t not in unpinned)
    result["left_alone"] = sorted(t for t in tids if t in unpinned or t not in assignments)

    for tid, cpus in sorted(assignments.items()):
        if tid not in tidset:
            continue
        if dry_run:
            result["pinned"][tid] = cpus
            continue
        try:
            os.sched_setaffinity(tid, cpus)  # on Linux a thread id works as the "pid" here
            result["pinned"][tid] = cpus
        except (ProcessLookupError, FileNotFoundError):
            result["failed"][tid] = "thread exited"
        except OSError as e:
            result["failed"][tid] = str(e)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pid", type=int, help="process id whose threads to pin")
    ap.add_argument("--strategy", choices=["default", "round-robin", "plan"], default="default")
    ap.add_argument("--plan", help="plan JSON file (for --strategy plan)")
    ap.add_argument("--cpus", type=int, help="CPU count for round-robin (default: all CPUs)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be pinned, change nothing")
    args = ap.parse_args()

    try:
        r = apply(args.pid, args.strategy, args.plan, args.cpus, args.dry_run)
    except FileNotFoundError as e:
        print(f"error: {e.filename or e}: process or file not found", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    verb = "would pin" if args.dry_run else "pinned"
    print(f"strategy={r['strategy']} pid={r['pid']}")
    for tid, cpus in sorted(r["pinned"].items()):
        print(f"  {verb} thread {tid} -> CPUs {cpus}")
    for tid, why in sorted(r["failed"].items()):
        print(f"  FAILED thread {tid}: {why}")
    if r["left_alone"]:
        print(f"  left alone: {r['left_alone']}")
    if r["missing_from_plan"]:
        print(f"  warning: threads in the process but not in the plan: {r['missing_from_plan']}")
    if r["plan_tids_not_found"]:
        print(f"  warning: plan threads not in the process: {r['plan_tids_not_found']}")
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
