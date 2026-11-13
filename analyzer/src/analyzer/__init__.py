"""Userspace analyzer: infer thread relationship graphs and score them.

This package never talks to a live collector. The collector writes a finished
trace file; the analyzer reads that file later. Do not run it during overhead
or placement experiments — it would compete for CPUs and add noise.

Pipeline:
  1. ``analyzer infer TRACE --method wakeup|timing`` reads only the trace and
     writes ``graph.json`` (optional Graphviz DOT).
  2. ``analyzer score GRAPH --groundtruth PREFIX`` reads only the graph and the
     benchmark CSVs. Separating the commands makes ground-truth leakage
     impossible by construction.

See ``analyzer/README.md`` and ``docs/event-format.md``.
"""

from analyzer.cli import main as _cli_main


def main() -> None:
    """Entry point for the ``analyzer`` console script (see pyproject.toml)."""
    raise SystemExit(_cli_main())
