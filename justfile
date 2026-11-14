# Analyzer command runner.
# Install: uv tool install rust-just
# Then:    just --list
#
# Virtualenv lives at the repo-root .venv (uv workspace). Cursor auto-activates it.

set shell := ["bash", "-euo", "pipefail", "-c"]

analyzer_directory := "analyzer"

# List available recipes.
default:
    @just --list

# Sync the workspace virtualenv (analyzer + dev extras: pytest).
sync:
    uv sync --package analyzer --extra dev

# Run analyzer unit tests.
test: sync
    cd {{analyzer_directory}} && uv run pytest

# Infer a relationship graph from a trace.
# Example: just infer /tmp/traced_run.events.bin --method wakeup -o wakeup.json
# Also writes wakeup.dot next to the JSON (override with --dot PATH).
infer *args: sync
    uv run analyzer infer {{args}}

# Score a graph against benchmark ground truth.
# Example: just score analyzer/wakeup.json --groundtruth groundtruth
score *args: sync
    uv run analyzer score {{args}}

# Compare wakeup vs timing across runs listed in a manifest CSV.
# Manifest columns: workload,trace_path,groundtruth_prefix
# Example: just compare manifest.csv --timing-windows 25,50,100,250,500,1000 --csv runs.csv
compare *args: sync
    uv run analyzer compare {{args}}
