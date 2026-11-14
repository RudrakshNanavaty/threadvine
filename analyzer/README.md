# Analyzer

Offline userspace tool that turns a collector trace into a thread relationship
graph and scores it against the benchmark ground truth.

The analyzer never runs during tracing. Point it at a finished `.events.bin`
(or a hand-written `.jsonl`) after the collector exits.

## Setup

From the **repo root** (uv workspace; `.venv` is created there for Cursor):

```
uv sync --package analyzer --extra dev
# or: just sync
```

## Commands

From the repo root:

```
uv run analyzer infer TRACE --method wakeup|timing [-o graph.json] [--dot FILE]
# DOT defaults to the same stem as -o (graph.json -> graph.dot)
uv run analyzer score graph.json --groundtruth PREFIX
# or: just infer … / just score …
```

`infer` reads only the trace. `score` reads only the graph and the benchmark's
`PREFIX.threads.csv` / `PREFIX.handoffs.csv`. They never share a process with
the collector.

### Example with the validation benchmark

```
# terminal 1
./build/pipeline -d 5 -w 2 -g 2 -n 40 -p 2000 -c 1000 -o groundtruth

# terminal 2 (after it prints its pid)
make run ARGS="-o traced_run.events.bin <pid>"

# after both finish (repo root)
uv run analyzer infer traced_run.events.bin --method wakeup -o analyzer/wakeup.json
# also writes analyzer/wakeup.dot; render with: dot -Tpng analyzer/wakeup.dot -o wakeup.png
uv run analyzer score analyzer/wakeup.json --groundtruth groundtruth
```

## Comparing methods

```bash
# manifest.csv columns: workload,trace_path,groundtruth_prefix (repeats share a workload)
uv run analyzer compare manifest.csv --csv runs.csv
uv run analyzer compare manifest.csv --timing-windows 25,50,100,250,500,1000   # window sweep
```

`compare` runs wakeup and timing with the same frozen thresholds and prints a Markdown
table (mean and min-max over repeats). Relative manifest paths resolve against the
manifest's directory.

Frozen timing window: _not yet chosen_ — set after the calibration sweep on the VM.

## Score-coloured figures

```bash
uv run analyzer score graph.json --groundtruth P --dot scored.dot
uv run analyzer compare manifest.csv --dot-directory figures/
```

Confident edges are green when true and red when false; true edges the graph missed are
drawn dashed grey. Colours come from the same `score_graph` result as the tables.

## Methods

| Method | Evidence | Notes |
| --- | --- | --- |
| `wakeup` | `sched_waking` waker → woken | Dominant direction per unordered pair; carries `reverse_wakeup_count`. |
| `timing` | Block (`previous_thread_state != 0`) then another target thread switches in within `--timing-window-us` | Baseline that uses no wakeup information. |

A combined futex / pipe-socket method is planned for milestone 2.

## Confidence thresholds (frozen after calibration)

An edge is **confident** when both hold:

- `count >= --minimum-count` (default **5**)
- the edge is at least `--minimum-fraction` (default **0.05**) of the
  destination thread's incoming evidence

Otherwise it is **ambiguous**: listed in the graph and DOT (dashed), but not
counted in precision / recall.

Calibration run (`-g 2 -w 2 -n 40`, sleep mode): wakeup method reached
directed precision and recall of 1.0. Edges from `pthread_join` into `main`
and any cross-group noise stayed ambiguous under these defaults, so no
`--skip-first-ms` trim was added.

## Scoring

Headline numbers are **directed** precision and recall on the confident edge
set. Undirected figures, cross-group confident edges, edges involving `main`,
missed true edges, and ambiguous-edge count are reported alongside. Lossy
traces (dropped events or missing `END_OF_TRACE`) print a warning above the
numbers.

## Tests

```
just test
# or: cd analyzer && uv run pytest
```

Hand-written `.jsonl` fixtures cover ordering, reverse wakeups, outside-process
wakers, missing end-of-trace, and threshold boundaries. A short real capture
lives in `tests/fixtures/real_run/`.

## Layout

| Module | Role |
| --- | --- |
| `event_decoder.py` | Binary / `.jsonl` decoder (also used by `bpf/tools/read_events.py`) |
| `trace.py` | `prepare_trace`: sort, pick target process, keep events that involve it |
| `wakeup_inference.py` / `timing_inference.py` | `infer(trace, settings) -> Graph` |
| `graph.py` | Edge labels, JSON and DOT output |
| `scoring.py` | Ground-truth comparison |
| `cli.py` | `infer` and `score` subcommands |
