#!/usr/bin/env python3
"""Run the benchmark WITH the eBPF collector, infer thread relationships, and score them.

For each configuration and repeat this does:
  1. start ./build/pipeline (it waits --attach-delay seconds before working),
  2. start the collector on that process (runs as root through sudo),
  3. wait for both to finish (the collector exits by itself when the benchmark ends),
  4. check the ground-truth files with bench/check_groundtruth.py,
  5. infer a graph with BOTH methods (wakeup and timing) and score each against the answer key.
One CSV row is written per run. A run counts as valid only if the benchmark and collector
exited 0, the answer key is consistent, the trace ends with its END_OF_TRACE record and
no events were dropped. The timing method is the baseline for the wakeup method.

Unless --no-untraced is given, every traced run is paired with an untraced run of the same
configuration (same round), so the collector's overhead can be computed by the summarizer.

Needs (on the VCL VM, from the repo root):
  make bpf && make bench        # builds build/collector and build/pipeline
  source ~/venv-os/bin/activate # an environment where `pip install -e ./analyzer` was done
  sudo -v                       # the collector needs root; this runner never asks for a password

Usage:
  python3 experiments/run_traced.py --list
  python3 experiments/run_traced.py --configs slow_w2g2,sat_w2g2 --repeats 5
  python3 experiments/run_traced.py --configs all --repeats 5

Results go to experiments/results/ (git-ignored). Summarize with summarize_traced.py.
"""
import argparse
import csv
import getpass
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, os.path.join(ROOT, "analyzer", "src"))
import check_groundtruth as cg  # noqa: E402
import run_experiments as rx  # noqa: E402

try:
    from analyzer import timing_inference, wakeup_inference
    from analyzer.graph import InferenceSettings
    from analyzer.scoring import score_graph
    from analyzer.trace import load_trace
    ANALYZER_ERROR = None
except ImportError as exc:  # typer missing, or analyzer not installed
    ANALYZER_ERROR = str(exc)

# name -> pipeline options (sleep mode unless the name says spin). slow = the producer is
# slower than the workers; sat = no pause between arrivals, so the queue is always full.
def _cfg(w, g, p, m="sleep", n=200, c=1000, q=8):
    return dict(w=w, g=g, n=n, p=p, c=c, m=m, q=q)

CONFIGS = {
    "slow_w1g1":      _cfg(1, 1, 2000),
    "slow_w4g1":      _cfg(4, 1, 2000),
    "slow_w2g2":      _cfg(2, 2, 2000),
    "sat_w1g1":       _cfg(1, 1, 0),
    "sat_w4g1":       _cfg(4, 1, 0),
    "sat_w2g2":       _cfg(2, 2, 0),
    "slow_spin_w2g2": _cfg(2, 2, 2000, m="spin"),
    "sat_spin_w2g2":  _cfg(2, 2, 0, m="spin"),
}
METHODS = ("wakeup", "timing")
SCORE_FIELDS = ["confident", "ambiguous", "dir_precision", "dir_recall", "undir_precision",
                "undir_recall", "dir_tp", "dir_fp", "dir_fn", "cross_group", "main_edges"]
COLUMNS = (["config", "repeat", "traced", "timestamp"] + rx.OPT_COLUMNS + ["attach_delay_s"] +
           ["elapsed_s", "throughput", "p50_ms", "p95_ms", "p99_ms", "max_ms",
            "events_recorded", "events_dropped", "trace_complete", "validation"] +
           [f"{m}_{f}" for m in METHODS for f in SCORE_FIELDS] +
           ["valid", "note"])


def fmt(value):
    return "" if value is None else (f"{value:.4f}" if isinstance(value, float) else value)


def analyze(trace_path, prefix):
    """Infer with both methods and score. Returns (fields dict, trace-loss dict)."""
    trace = load_trace(trace_path)
    fields, loss = {}, {}
    for method, module in (("wakeup", wakeup_inference), ("timing", timing_inference)):
        graph = module.infer(trace, InferenceSettings(method=method))
        rep = score_graph(graph, prefix)
        loss = {"recorded": graph.metadata.recorded_event_count,
                "dropped": graph.metadata.dropped_event_count,
                "complete": graph.metadata.has_end_of_trace}
        values = dict(confident=rep.confident_edge_count, ambiguous=rep.ambiguous_edge_count,
                      dir_precision=rep.directed_precision, dir_recall=rep.directed_recall,
                      undir_precision=rep.undirected_precision, undir_recall=rep.undirected_recall,
                      dir_tp=rep.directed_true_positives, dir_fp=rep.directed_false_positives,
                      dir_fn=rep.directed_false_negatives,
                      cross_group=rep.cross_group_confident_edge_count,
                      main_edges=rep.main_confident_edge_count)
        for f in SCORE_FIELDS:
            fields[f"{method}_{f}"] = fmt(values[f])
    return fields, loss


def attached_in_time(trace_path, prefix):
    """True if the collector started tracing before the benchmark logged its first handoff.
    The trace header holds a CLOCK_MONOTONIC start time (the clock the benchmark logs with)."""
    with open(trace_path, "rb") as f:
        header = f.read(32)
    magic, _version, _size, _clock, _target, _reserved, start_ns = struct.unpack("=4sIIIIIQ", header)
    if magic != b"TRPF":
        return False
    with open(f"{prefix}.handoffs.csv", newline="") as f:
        first_put = min(int(r["put_ns"]) for r in csv.DictReader(f))
    return start_ns < first_put


def sudo(args, use_sudo):
    return (["sudo", "-n"] if use_sudo else []) + args


def traced_run(name, opts, repeat, a, collector_path):
    row = {"config": name, "repeat": repeat, "traced": "yes", "attach_delay_s": a.attach_delay,
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **{k: opts[k] for k in rx.OPT_COLUMNS},
           "note": "", "valid": "no"}
    tmp = tempfile.mkdtemp(prefix="traced_")
    prefix = os.path.join(tmp, "gt")
    trace = tmp + ".events.bin"
    use_sudo = not a.no_sudo
    notes = []
    try:
        if use_sudo:  # refresh the sudo timestamp so long batches do not hit a password prompt
            subprocess.run(["sudo", "-n", "-v"], check=True, capture_output=True)
        cmd = [a.pipeline, "-d", str(a.attach_delay)] + \
              [x for k in rx.OPT_COLUMNS for x in (f"-{k}", str(opts[k]))] + ["-o", prefix]
        bench = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        coll = subprocess.Popen(sudo([collector_path, "--output", trace, str(bench.pid)], use_sudo),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            out, err = bench.communicate(timeout=a.attach_delay + a.timeout)
        except subprocess.TimeoutExpired:
            bench.kill()
            out, err = bench.communicate()
            notes.append("benchmark timed out")
        try:
            cerr = coll.communicate(timeout=30)[1]
        except subprocess.TimeoutExpired:
            coll.send_signal(2)  # SIGINT, relayed by sudo
            try:
                cerr = coll.communicate(timeout=10)[1]
            except subprocess.TimeoutExpired:
                cerr = ""
            notes.append("collector did not exit by itself")
        row.update(rx.parse_summary(out))
        if bench.returncode != 0:
            notes.append(f"benchmark exit {bench.returncode}: " + ((err.strip().splitlines() or [""])[-1]))
        if coll.returncode != 0:
            notes.append(f"collector exit {coll.returncode}: " + ((cerr.strip().splitlines() or [""])[-1]))
        if not notes:
            if use_sudo:
                subprocess.run(["sudo", "-n", "chmod", "a+r", trace], check=True, capture_output=True)
            res = cg.check(prefix, items=opts["n"], groups=opts["g"], workers=opts["w"])
            row["validation"] = "FAIL" if res["errors"] else "OK"
            if res["errors"]:
                notes.append("answer key: " + res["errors"][0])
            if not attached_in_time(trace, prefix):
                notes.append("collector attached after the benchmark started (raise --attach-delay)")
            fields, loss = analyze(trace, prefix)
            row.update(fields)
            row.update(events_recorded=loss["recorded"], events_dropped=loss["dropped"],
                       trace_complete="yes" if loss["complete"] else "no")
            if loss["dropped"]:
                notes.append(f"{loss['dropped']} events dropped")
            if not loss["complete"]:
                notes.append("trace has no END_OF_TRACE record")
            if not notes:
                row["valid"] = "yes"
    except subprocess.CalledProcessError:
        notes.append("sudo needs a password: run `sudo -v` and start again")
    except Exception as exc:  # keep the batch going, record why this run failed
        notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        row["note"] = "; ".join(notes)
        if a.keep:
            row["note"] = (row["note"] + f" kept {tmp} and {trace}").strip()
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            if os.path.exists(trace):
                subprocess.run(sudo(["rm", "-f", trace], use_sudo), capture_output=True)
    return row


def untraced_run(name, opts, repeat, a):
    row = rx.run_once(a.pipeline, name, opts, repeat, False, a.timeout)
    row["traced"] = "no"
    row["attach_delay_s"] = 0
    row.pop("exit_code", None)
    row.pop("handoffs", None)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--configs", default="slow_w2g2,sat_w2g2")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--attach-delay", type=int, default=5, help="seconds the benchmark waits for the collector")
    ap.add_argument("--timeout", type=int, default=120, help="seconds allowed per run after the delay")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-untraced", action="store_true", help="skip the paired untraced runs (no overhead numbers)")
    ap.add_argument("--keep", action="store_true", help="keep traces and ground-truth files of every run")
    ap.add_argument("--pipeline", default=os.path.join(ROOT, "build", "pipeline"))
    ap.add_argument("--collector", default=None, help="collector program (default: build/collector, copied to /tmp)")
    ap.add_argument("--no-sudo", action="store_true", help="run the collector without sudo (testing only)")
    a = ap.parse_args()

    if a.list:
        for name, o in CONFIGS.items():
            print(f"{name:15} " + " ".join(f"-{k} {o[k]}" for k in rx.OPT_COLUMNS))
        return 0
    names = list(CONFIGS) if a.configs == "all" else [s.strip() for s in a.configs.split(",") if s.strip()]
    unknown = [n for n in names if n not in CONFIGS]
    if unknown or a.repeats < 1:
        print(f"error: unknown config(s) {unknown} or bad --repeats; use --list", file=sys.stderr)
        return 2
    if ANALYZER_ERROR:
        print(f"error: cannot import the analyzer ({ANALYZER_ERROR}).\n"
              "Activate the environment with `pip install -e ./analyzer` done.", file=sys.stderr)
        return 2
    if not os.path.exists(a.pipeline):
        print(f"error: {a.pipeline} not found; run `make bench`", file=sys.stderr)
        return 2

    collector_path = a.collector
    if collector_path is None:
        built = os.path.join(ROOT, "build", "collector")
        if not os.path.exists(built):
            print("error: build/collector not found; run `make bpf`", file=sys.stderr)
            return 2
        # Root cannot read a repo on the NFS drive, so run a local copy (same as `make run`).
        run_dir = f"/tmp/ebpf-collector-{getpass.getuser()}"
        os.makedirs(run_dir, exist_ok=True)
        collector_path = os.path.join(run_dir, "collector")
        shutil.copy2(built, collector_path)
        os.chmod(collector_path, 0o755)
    if not a.no_sudo and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        print("error: sudo needs a password. Run `sudo -v` first, then start this again.", file=sys.stderr)
        return 2

    out = a.out or os.path.join(HERE, "results", time.strftime("traced_%Y%m%d_%H%M%S.csv"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    bad = 0
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        for rep in range(1, a.repeats + 1):  # interleaved: every config once per round
            for name in names:
                if not a.no_untraced:
                    r = untraced_run(name, CONFIGS[name], rep, a)
                    w.writerow(r)
                    f.flush()
                    bad += r["valid"] != "yes"
                row = traced_run(name, CONFIGS[name], rep, a, collector_path)
                w.writerow(row)
                f.flush()
                bad += row["valid"] != "yes"
                print(f"[{rep}/{a.repeats}] {name:15} valid={row['valid']:3} "
                      f"wakeup dir P/R={row.get('wakeup_dir_precision', '-')}/{row.get('wakeup_dir_recall', '-')} "
                      f"undir P/R={row.get('wakeup_undir_precision', '-')}/{row.get('wakeup_undir_recall', '-')} "
                      f"{row['note']}", flush=True)
    print(f"wrote {out}  ({bad} invalid run(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
