# Ground-Truth Format

The answer key written by the validation benchmark (`bench/pipeline.c`) and
read by the analyzer **only for scoring**. The analyzer must never use it to
infer relationships.

**Status: DRAFT v0.** Agree on this with the collector and analyzer owners
before either side depends on it.

## What the benchmark does

One pipeline ("group") is three kinds of threads connected by two bounded queues:

```
producer --queue1--> worker(s) --queue2--> sink
```

`-g` runs several groups side by side. Groups never exchange items, so a
correct analyzer must draw **no edge between two groups**.

## Definition of a true edge

> There is a true edge A -> B if thread A put at least one item into a queue
> and thread B took that item out.

- Edges are between **threads** (kernel thread IDs). Roll them up to roles or
  to the process yourself if you need to.
- With more than one worker, the OS decides which worker takes each item, so
  the set of true edges can differ from run to run. Always score a run against
  **that run's** files.
- A true edge does **not** guarantee a scheduler wakeup. If the receiving
  thread is already awake when an item arrives, it takes the item without
  sleeping, and the kernel sees no wakeup. Expect some true edges to be weak or
  invisible in wakeup data; report them as missed, do not hide them.

## Files

Written next to where you run `./pipeline` (prefix set with `-o`, default
`groundtruth`), after the timed part of the run ends.

### `PREFIX.threads.csv`: which thread is which

| Column | Meaning |
| --- | --- |
| `tid` | Kernel thread ID, from `gettid()`. This is what the collector records. |
| `role` | `main`, `producer`, `worker` or `sink` |
| `group` | Pipeline number (`-1` for `main`) |
| `index` | Worker number inside the group (0 for the others) |

### `PREFIX.handoffs.csv`: one row per handoff, sorted by `get_ns`

| Column | Meaning |
| --- | --- |
| `group` | Pipeline number |
| `item_id` | Item number inside the group (0, 1, 2, ...) |
| `from_tid` | Thread that put the item in the queue |
| `to_tid` | Thread that took it out |
| `stage` | `producer_to_worker` or `worker_to_sink` |
| `put_ns` | When the sender put it in the queue |
| `get_ns` | When the receiver took it out |
| `phase` | `timed` for the measured run, `profile` for the profile phase (`-W`). Always `timed` without `-W`. |

Each item produces exactly two rows: one per stage. Item numbers start at 0 again in each phase, so an item is identified by `group`, `phase` and `item_id`.

## Clock

All times are `clock_gettime(CLOCK_MONOTONIC)` in nanoseconds. This is the same
clock as `bpf_ktime_get_ns()` in the collector, so the numbers can be compared
directly. Do not use wall-clock time.

## Running it with the collector

1. Start the benchmark with a delay so you can attach the collector. It prints
   its process ID first: `./build/pipeline -d 10 -w 2 -p 2000 -c 3000`
2. In another terminal, run the collector on that ID: `make run ARGS=<pid>`
3. When the benchmark finishes it writes the two CSV files.
4. Check them and print the true graph: `python3 bench/check_groundtruth.py`

## Benchmark options

| Option | Meaning | Default |
| --- | --- | --- |
| `-w N` | worker threads per group | 1 |
| `-g N` | independent pipelines | 1 |
| `-n N` | items per group | 200 |
| `-p US` | microseconds between arrivals (0 = no pause) | 10000 |
| `-c US` | work per item in a worker, microseconds | 5000 |
| `-m MODE` | `sleep` (uses no CPU) or `spin` (burns CPU time) | sleep |
| `-q N` | queue capacity | 8 |
| `-d S` | seconds to wait before starting | 0 |
| `-W S` | profile phase of S seconds, then wait for SIGUSR1 and start a fresh timed interval (see below) | 0 (off) |
| `-o PREFIX` | ground-truth file prefix | groundtruth |

`spin` mode counts the thread's own CPU time, so the amount of work stays fixed
even if the thread is preempted. Use it for the CPU-contention experiments;
`sleep` mode uses almost no CPU.

## Profile phase (`-W S`)

Used when something has to happen to the running threads between tracing them and
measuring them, for example pinning them to CPUs from a plan built on the trace.

1. The threads run the normal workload for `S` seconds (the profile phase). Attach the
   collector before this starts (`-d`), and build the thread graph from its trace.
2. The benchmark drains the profile items and prints `waiting for SIGUSR1 (kill -USR1 <pid>)`.
   The threads are idle now. Apply the CPU pinning here. The thread ids stay the same.
3. Send `kill -USR1 <pid>`. A signal sent earlier is not lost: it is used as soon as the
   profile phase has drained.
4. A fresh timed interval of `-n` items per group runs on the same process. Only this
   interval counts for throughput and latency. The summary line describes it alone.
5. The answer key holds both phases (column `phase`). Profile-phase items are in the key
   so the graph built from the profile can be scored. `check_groundtruth.py --items N`
   checks the timed items against `N`; the profile items are checked only for gaps.

## Summary line

The benchmark also prints throughput and end-to-end latency (p50, p95, p99,
max). Latency is measured from when an item was **due** to arrive, not from when
the producer managed to send it, so a slow pipeline cannot hide its delay.
