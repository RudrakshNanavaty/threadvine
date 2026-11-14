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
