#!/usr/bin/env python3
"""Measure collector overhead on the ping-pong stress test (the worst case for Part A).

bpf/tests/pingpong.c makes two threads pass a turn back and forth as fast as they can,
so every round is two context switches and two wakeups: about 5.7 events per round.
That is far more events per second than the pipeline benchmark produces, so this gives
the upper bound on what tracing costs.

Each repeat runs every mode once, in a rotating order so slow drift on the VM affects
all modes alike:
  untraced  pingpong alone
  binary    collector --output FILE (the format the team uses)
  text      collector printing one text line per event, redirected to a file

Throughput = rounds / seconds from pressing Enter until pingpong exits.
Overhead   = how much lower the traced throughput is than the untraced throughput.

Needs (on the VCL VM, from the repo root):
  make bpf     # builds build/collector
  sudo -v      # the collector needs root; this script never asks for a password

Usage:
  python3 experiments/pingpong_overhead.py
  python3 experiments/pingpong_overhead.py --rounds 200000 --repeats 5 --modes untraced,binary,text

Results go to experiments/results/ (git-ignored).
"""
import argparse
import csv
import os
import re
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import time

EXPERIMENTS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
REPOSITORY_ROOT = os.path.dirname(EXPERIMENTS_DIRECTORY)
PINGPONG_SOURCE = os.path.join(REPOSITORY_ROOT, "bpf", "tests", "pingpong.c")
PINGPONG_PROGRAM = os.path.join(REPOSITORY_ROOT, "build", "pingpong")
MODES = ("untraced", "binary", "text")

# bpf/event.h struct event_file_header: magic, format_version, record_size_bytes,
# clock_identifier, target_process_identifier, reserved, start_timestamp_nanoseconds
FILE_HEADER = struct.Struct("=4sIIIIIQ")
COLLECTOR_SUMMARY = re.compile(r"events: (\d+) dropped: (\d+)")

COLUMNS = ["mode", "repeat", "rounds", "elapsed_seconds", "rounds_per_second",
           "recorded_event_count", "dropped_event_count", "valid", "note"]


def build_pingpong():
    """Compile bpf/tests/pingpong.c into build/pingpong if it is missing or out of date."""
    if (os.path.exists(PINGPONG_PROGRAM)
            and os.path.getmtime(PINGPONG_PROGRAM) >= os.path.getmtime(PINGPONG_SOURCE)):
        return
    os.makedirs(os.path.dirname(PINGPONG_PROGRAM), exist_ok=True)
    subprocess.run(["cc", "-O2", "-Wall", "-pthread", PINGPONG_SOURCE, "-o", PINGPONG_PROGRAM],
                   check=True)


def copy_collector(collector_path, working_directory):
    """Run a local copy: the repo may be on NFS, where root cannot read files (see `make run`)."""
    local_copy = os.path.join(working_directory, "collector")
    shutil.copy(collector_path, local_copy)
    os.chmod(local_copy, 0o755)
    return local_copy


def trace_started_before(trace_path, start_monotonic_nanoseconds):
    """True if the collector's file header says tracing began before pingpong started work."""
    try:
        with open(trace_path, "rb") as trace_file:
            header = trace_file.read(FILE_HEADER.size)
    except OSError:
        return False
    if len(header) < FILE_HEADER.size:
        return False
    magic, *_unused, start_timestamp_nanoseconds = FILE_HEADER.unpack(header)
    return magic == b"TRPF" and start_timestamp_nanoseconds < start_monotonic_nanoseconds


def stop_collector(collector_process):
    """Wait for the collector to exit by itself (it watches pingpong); interrupt it if not."""
    try:
        return collector_process.communicate(timeout=30)[1], None
    except subprocess.TimeoutExpired:
        collector_process.send_signal(signal.SIGINT)  # sudo relays it to the collector
        try:
            return collector_process.communicate(timeout=10)[1], "collector did not exit by itself"
        except subprocess.TimeoutExpired:
            collector_process.kill()
            return "", "collector had to be killed"


def run_once(mode, repeat, arguments, collector, working_directory):
    row = {column: "" for column in COLUMNS}
    row.update(mode=mode, repeat=repeat, rounds=arguments.rounds, valid="no")
    trace_path = os.path.join(working_directory, f"repeat{repeat}_{mode}.events.bin")
    text_path = os.path.join(working_directory, f"repeat{repeat}_{mode}.txt")
    notes = []
    text_file = None
    collector_process = None

    # pingpong waits on stdin for Enter, so the collector can attach before any round runs.
    pingpong = subprocess.Popen([PINGPONG_PROGRAM, str(arguments.rounds)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    try:
        if mode != "untraced":
            command = ["sudo", "-n", collector]
            if mode == "binary":
                command += ["--output", trace_path]
                collector_stdout = subprocess.DEVNULL
            else:
                text_file = open(text_path, "w")
                collector_stdout = text_file
            collector_process = subprocess.Popen(command + [str(pingpong.pid)],
                                                 stdout=collector_stdout,
                                                 stderr=subprocess.PIPE, text=True)

        # The same pause in every mode, so the runs differ only in whether the collector runs.
        time.sleep(arguments.attach_wait_seconds)
        if collector_process is not None and collector_process.poll() is not None:
            error_lines = collector_process.stderr.read().strip().splitlines()
            notes.append("collector exited early: " + (error_lines[-1] if error_lines else "?"))
            pingpong.kill()
            pingpong.wait()
            return row, notes

        start_monotonic_nanoseconds = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        start_seconds = time.perf_counter()
        pingpong.stdin.write(b"\n")
        pingpong.stdin.close()
        try:
            pingpong.wait(timeout=arguments.timeout_seconds)
        except subprocess.TimeoutExpired:
            pingpong.kill()
            pingpong.wait()
            notes.append("pingpong timed out")
        elapsed_seconds = time.perf_counter() - start_seconds

        if pingpong.returncode == 0 and not notes:
            row["elapsed_seconds"] = f"{elapsed_seconds:.4f}"
            row["rounds_per_second"] = f"{arguments.rounds / elapsed_seconds:.1f}"
        elif not notes:
            notes.append(f"pingpong exit {pingpong.returncode}")

        if collector_process is not None:
            collector_errors, stop_note = stop_collector(collector_process)
            if stop_note:
                notes.append(stop_note)
            summary = COLLECTOR_SUMMARY.search(collector_errors or "")
            if summary:
                row["recorded_event_count"] = summary.group(1)
                row["dropped_event_count"] = summary.group(2)
                if int(summary.group(2)) > 0:
                    notes.append("events were dropped")
            else:
                notes.append("no event summary from the collector")
            if collector_process.returncode != 0:
                notes.append(f"collector exit {collector_process.returncode}")
            if mode == "binary" and not trace_started_before(trace_path, start_monotonic_nanoseconds):
                notes.append("collector was not attached in time (raise --attach-wait-seconds)")
    finally:
        if text_file is not None:
            text_file.close()
        if not arguments.keep:
            for path in (trace_path, text_path):
                if os.path.exists(path):
                    os.remove(path)

    if not notes:
        row["valid"] = "yes"
    return row, notes


def summarize(rows, modes):
    def valid_values(mode, column):
        return [float(row[column]) for row in rows
                if row["mode"] == mode and row["valid"] == "yes" and row[column] != ""]

    untraced_rates = valid_values("untraced", "rounds_per_second")
    baseline = statistics.mean(untraced_rates) if untraced_rates else None
    print("\nPing-pong collector overhead (negative overhead = within noise)")
    print(f"{'mode':9} {'valid runs':>10} {'rounds/s':>12} {'spread':>8} {'overhead':>9}"
          f" {'events/run':>11} {'dropped':>8}")
    for mode in modes:
        rates = valid_values(mode, "rounds_per_second")
        run_count = sum(1 for row in rows if row["mode"] == mode)
        if not rates:
            print(f"{mode:9} {f'0/{run_count}':>10}  no valid runs")
            continue
        mean_rate = statistics.mean(rates)
        spread = (max(rates) - min(rates)) / mean_rate * 100
        if mode == "untraced" or baseline is None:
            overhead = "-"
        else:
            overhead = f"{(1 - mean_rate / baseline) * 100:.1f}%"
        events = valid_values(mode, "recorded_event_count")
        dropped = valid_values(mode, "dropped_event_count")
        events_text = f"{statistics.mean(events):,.0f}" if events else "-"
        dropped_text = f"{sum(dropped):.0f}" if dropped else "-"
        print(f"{mode:9} {f'{len(rates)}/{run_count}':>10} {mean_rate:>12,.0f} {spread:>7.1f}%"
              f" {overhead:>9} {events_text:>11} {dropped_text:>8}")
    print("spread = (max - min) / mean over valid runs; an overhead smaller than the spread is noise.")


def main():
    parser = argparse.ArgumentParser(description="Collector overhead on the ping-pong stress test.")
    parser.add_argument("--rounds", type=int, default=200000, help="ping-pong rounds per run")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--modes", default=",".join(MODES), help="comma-separated: " + ", ".join(MODES))
    parser.add_argument("--attach-wait-seconds", type=float, default=2.0,
                        help="pause before starting pingpong, so the collector can attach")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--collector", default=os.path.join(REPOSITORY_ROOT, "build", "collector"))
    parser.add_argument("--output", default=None,
                        help="CSV file (default: experiments/results/pingpong_overhead_<time>.csv)")
    parser.add_argument("--keep", action="store_true", help="keep the trace and text files")
    arguments = parser.parse_args()

    modes = [mode.strip() for mode in arguments.modes.split(",") if mode.strip()]
    unknown_modes = [mode for mode in modes if mode not in MODES]
    if unknown_modes or not modes:
        sys.exit(f"unknown modes: {', '.join(unknown_modes) or '(none given)'}")
    traced = any(mode != "untraced" for mode in modes)
    if traced:
        if not os.path.exists(arguments.collector):
            sys.exit(f"{arguments.collector} not found: run `make bpf` first")
        if subprocess.run(["sudo", "-n", "-v"], capture_output=True).returncode != 0:
            sys.exit("sudo needs a password: run `sudo -v` and start again")

    build_pingpong()
    working_directory = tempfile.mkdtemp(prefix="pingpong_overhead_")
    collector = copy_collector(arguments.collector, working_directory) if traced else None
    output_path = arguments.output or os.path.join(
        EXPERIMENTS_DIRECTORY, "results", time.strftime("pingpong_overhead_%Y%m%d_%H%M%S.csv"))
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    rows = []
    with open(output_path, "w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=COLUMNS)
        writer.writeheader()
        for repeat in range(1, arguments.repeats + 1):
            if traced:  # refresh the sudo timestamp so long batches never hit a password prompt
                subprocess.run(["sudo", "-n", "-v"], capture_output=True)
            offset = (repeat - 1) % len(modes)
            for mode in modes[offset:] + modes[:offset]:
                row, notes = run_once(mode, repeat, arguments, collector, working_directory)
                row["note"] = "; ".join(notes)
                rows.append(row)
                writer.writerow(row)
                output_file.flush()
                rate = row["rounds_per_second"] or "-"
                print(f"repeat {repeat} {mode:9} rounds/s {rate:>10}  events {row['recorded_event_count'] or '-':>8}"
                      f"  dropped {row['dropped_event_count'] or '-':>4}  {row['note']}")

    if arguments.keep:
        print(f"kept trace and text files in {working_directory}")
    else:
        shutil.rmtree(working_directory, ignore_errors=True)
    summarize(rows, modes)
    print(f"results: {output_path}")


if __name__ == "__main__":
    main()