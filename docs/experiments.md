# Running experiments (Part C)

Everything here needs only `build/pipeline` (run `make bench`). No eBPF collector or analyzer is needed.

## Run

    python3 experiments/run_experiments.py --list
    python3 experiments/run_experiments.py --configs paced_sleep,paced_spin --repeats 5
    python3 experiments/summarize_results.py experiments/results/<file>.csv --compare paced_sleep paced_spin

Use `--configs all` for every configuration. Rounds are interleaved (round 1 of every config, then round 2, ...), so slow changes on the machine affect all configs alike. Results go to `experiments/results/` (git-ignored).

## Configurations

| name | what it tests |
|---|---|
| basic, workers4, groups2 | correctness shapes: 1 worker, 4 workers, 2 independent groups |
| paced_sleep / paced_spin | arrivals slower than capacity; workers sleep vs burn CPU |
| unpaced_sleep / unpaced_spin | no pause between arrivals (`-p 0`); queue 8 |
| q1_sleep / q1_spin | same, queue capacity 1 (strong backpressure) |

## Results CSV columns

`config, repeat, timestamp, w, g, n, p, c, m, q` (the options used), `elapsed_s, throughput, p50_ms, p95_ms, p99_ms, max_ms, handoffs`, then `exit_code, validation, valid, note`.
A run is `valid=yes` only if the program exited 0 **and** the ground-truth validator passed with that run's `-n/-g/-w`. The summarizer ignores invalid runs and reports how many were dropped.

## Definitions

- **elapsed_s**: time from the start of the timed interval to when the last sink finishes; file writing is outside it.
- **Percentiles**: sort latencies ascending, take index `floor(q*n)` clamped to `n-1`, no interpolation. (Nearest-rank would be `ceil(q*n)-1`; they differ only when `q*n` is a whole number.)
- **Latency** is measured from each item's scheduled arrival time, so a backed-up queue shows up as latency.
- **spread%** in the summary = (max - min) / mean over the valid runs. Differences smaller than the spread are likely noise.

## Exit status of `pipeline`

0 = run finished and both ground-truth files were fully written. 1 = bad options, a thread could not be created, or a file write failed (the run is not valid).

## Validator

    python3 bench/check_groundtruth.py PREFIX --items N --groups G --workers W

Without `--items` it cannot see items missing from the end of a group; pass the same values used for the run.

## Traced experiments (benchmark + collector + analyzer)

`experiments/run_traced.py` runs the benchmark with the eBPF collector, infers the thread graph with both analyzer methods (`wakeup`, and the `timing` baseline) and scores each against the answer key.

Setup on the VCL VM (from the repo root): `./scripts/setup-vm.sh`, `make bpf && make bench`, a virtual environment with `pip install -e ./analyzer`, and `sudo -v` (the runner never asks for a password).

    python3 experiments/run_traced.py --list
    python3 experiments/run_traced.py --configs slow_w2g2,sat_w2g2 --repeats 5
    python3 experiments/summarize_traced.py experiments/results/traced_<time>.csv

Configurations: `slow_*` has a producer slower than the workers (`-p 2000`); `sat_*` has no pause between arrivals (`-p 0`), so the queue is always full. `w1g1`, `w4g1` and `w2g2` are workers and groups; `spin` means workers burn CPU instead of sleeping. 200 items per group, 1 ms of work per item.

A traced run is `valid=yes` only if the benchmark and collector exited 0, the answer key is consistent, the collector was attached before the first handoff, the trace has its END_OF_TRACE record, and no events were dropped. Each traced run is paired with an untraced run in the same round, so the summary can report collector overhead.

How to read the scores: directed scores need the right direction; undirected scores accept either direction. With a saturated producer the producer waits and the workers wake it, so the wakeup method can find the right pair but in the reverse direction. Report both. Edges between groups should be 0.

Extra columns per method: `reversed` (right pair, wrong direction), `contention` (two workers of the same group, which share the queue lock; a real dependency but not a handoff, so not in the answer key), `other_false`, and `adj_undir_precision` (undirected precision with the contention pairs left out of the count). The raw scores from the analyzer are unchanged; report both the raw and the adjusted numbers and say which is which.

## Pinning threads to CPUs

`experiments/apply_plan.py` pins the threads of a running process to CPUs, using `os.sched_setaffinity` on each thread under `/proc/<pid>/task`. It needs Linux and a live process.

    python3 experiments/apply_plan.py <pid> --strategy default
    python3 experiments/apply_plan.py <pid> --strategy round-robin --cpus 4
    python3 experiments/apply_plan.py <pid> --strategy plan --plan plan.json
    python3 experiments/apply_plan.py <pid> --strategy plan --plan plan.json --dry-run

Strategies: `default` changes nothing; `round-robin` gives the sorted thread ids CPUs 0, 1, 2, ... in turn; `plan` reads a plan file made by `analyzer plan`. `--dry-run` prints what would be pinned without changing anything.

Plan file format: `{"cpu_count": 4, "assignments": {"<tid>": [0, 1]}, "unpinned": [<tid>]}`. Threads in `unpinned` (in practice the main thread) are left alone. The script warns about threads in the process that are not in the plan, and about plan threads that are not in the process. It exits 0 if every pin worked, 1 if any failed, and 2 for bad input such as a missing process or a CPU the machine does not have.

Check the result with `taskset -cp <tid>`.

## Profile phase, then pin, then measure (`-W`)

`./build/pipeline -W S ...` runs the workload for `S` seconds as a profile phase, waits for `SIGUSR1`, then starts a fresh timed interval on the same process. Only that interval counts for throughput and p95 latency. Use it to compare placements on one process, because a plan is keyed on thread ids that only exist for the process that was traced.

    ./build/pipeline -d 5 -W 10 -g 2 -w 2 -m spin -p 0 -o /tmp/run &    # prints its pid
    # attach the collector during the -d delay, build the graph and plan from the trace
    python3 experiments/apply_plan.py <pid> --strategy plan --plan plan.json
    kill -USR1 <pid>                                                  # starts the timed interval

The profile items are in the answer key (column `phase`), so the graph built from the profile can still be scored. Details: `docs/ground-truth-format.md`.

cat >> docs/experiments.md <<'EOF'


## Collector overhead on the ping-pong stress test (Part A)

`experiments/pingpong_overhead.py` measures the worst case: `bpf/tests/pingpong.c` makes two threads hand a turn back and forth as fast as they can, about 270,000 events per second on the VCL VM. Each repeat runs `untraced`, `binary` (`collector --output`) and `text` (one printed line per event, sent to a file) once, in a rotating order.

    make bpf && sudo -v
    python3 experiments/pingpong_overhead.py --rounds 200000 --repeats 5

Throughput is rounds per second from start to exit. A traced run is `valid=yes` only if the collector exited 0, was attached before the first round, and dropped no events. The summary reports overhead against the untraced mean and the run-to-run spread; an overhead smaller than the spread is noise.

First result (4-vCPU VCL VM, 5 repeats): binary 8.9% throughput loss, text 10.4%, spread about 9%, 0 of about 11.6M events dropped. That is about 0.33 µs per event, so overhead grows with the event rate.
EOF
