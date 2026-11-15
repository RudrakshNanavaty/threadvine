"""Command-line interface: ``infer``, ``score``, ``compare`` and ``plan`` subcommands.

``infer`` never opens ground-truth files. ``score`` never opens a raw trace.
That split is intentional: relationship discovery must not peek at the answer
key (see ``docs/ground-truth-format.md``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from analyzer import timing_inference, wakeup_inference
from analyzer.compare import (
    DEFAULT_TIMING_WINDOW_US,
    format_comparison_table,
    read_manifest,
    run_comparison,
    write_results_csv,
)
from analyzer.graph import (
    DEFAULT_MINIMUM_COUNT,
    DEFAULT_MINIMUM_FRACTION,
    InferenceSettings,
    load_graph_json,
    write_graph_dot,
    write_graph_json,
)
from analyzer.plan import build_plan
from analyzer.scoring import format_score_report, score_graph, write_scored_graph_dot
from analyzer.trace import prepare_trace

app = typer.Typer(
    help="Infer and score thread relationship graphs from collector traces.",
    no_args_is_help=True,
)


@app.command()
def infer(
    trace: Annotated[
        Path,
        typer.Argument(help="Binary .events.bin or .jsonl trace file"),
    ],
    method: Annotated[
        Literal["wakeup", "timing"],
        typer.Option(help="Inference method"),
    ],
    output: Annotated[
        Path,
        typer.Option("-o", "--output", help="Output graph JSON path"),
    ] = Path("graph.json"),
    dot: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Graphviz DOT output path (default: same stem as --output with .dot)"
            ),
        ),
    ] = None,
    minimum_count: Annotated[
        int,
        typer.Option(
            help=(
                "Minimum evidence count for a confident edge "
                f"(default {DEFAULT_MINIMUM_COUNT})"
            ),
        ),
    ] = DEFAULT_MINIMUM_COUNT,
    minimum_fraction: Annotated[
        float,
        typer.Option(
            help=(
                "Minimum share of the destination thread's incoming evidence "
                f"(default {DEFAULT_MINIMUM_FRACTION})"
            ),
        ),
    ] = DEFAULT_MINIMUM_FRACTION,
    minimum_source_fraction: Annotated[
        float,
        typer.Option(
            help=(
                "Opt-in noise filter: minimum share of the source thread's "
                "outgoing evidence (default 0 = off)"
            ),
        ),
    ] = 0.0,
    timing_window_us: Annotated[
        int,
        typer.Option(
            help=(
                "Timing method: block-to-switch-in window in microseconds (default 100)"
            ),
        ),
    ] = DEFAULT_TIMING_WINDOW_US,
) -> int:
    """Infer a graph from a collector trace."""
    settings = InferenceSettings(
        method=method,
        minimum_count=minimum_count,
        minimum_fraction=minimum_fraction,
        timing_window_us=timing_window_us,
        minimum_source_fraction=minimum_source_fraction,
    )
    prepared_trace = prepare_trace(str(trace))
    if method == "wakeup":
        graph = wakeup_inference.infer(prepared_trace, settings)
    else:
        graph = timing_inference.infer(prepared_trace, settings)

    # Lossy traces still produce a graph; warnings travel into score via metadata.
    for warning in graph.loss_warnings():
        print(f"WARNING: {warning}", file=sys.stderr)
    if prepared_trace.outside_process_event_count:
        print(
            f"note: dropped {prepared_trace.outside_process_event_count} events involving "
            "threads outside the target process",
            file=sys.stderr,
        )

    output_path = str(output)
    write_graph_json(graph, output_path)
    print(
        f"wrote {output_path}: {len(graph.edges)} edges "
        f"({sum(1 for edge in graph.edges if edge.confidence == 'confident')} confident, "
        f"{sum(1 for edge in graph.edges if edge.confidence == 'ambiguous')} ambiguous)"
    )
    dot_path = str(dot) if dot is not None else str(output.with_suffix(".dot"))
    write_graph_dot(graph, dot_path)
    print(f"wrote {dot_path}")
    return 0


@app.command()
def score(
    graph: Annotated[
        Path,
        typer.Argument(help="graph.json produced by infer"),
    ],
    groundtruth: Annotated[
        str,
        typer.Option(help="Prefix of the benchmark ground-truth CSV files"),
    ],
    dot: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Also write a Graphviz DOT figure coloured by the score: "
                "true positive green, false positive red, missed edge dashed grey"
            ),
        ),
    ] = None,
) -> int:
    """Score a graph against ground truth."""
    loaded_graph = load_graph_json(str(graph))
    report = score_graph(loaded_graph, groundtruth)
    print(format_score_report(report))
    if dot is not None:
        write_scored_graph_dot(loaded_graph, report, str(dot))
        print(f"wrote {dot}")
    return 0


@app.command()
def compare(
    manifest: Annotated[
        Path,
        typer.Argument(
            help=(
                "CSV with columns workload, trace_path, groundtruth_prefix; "
                "one row per run, repeats share a workload name"
            ),
        ),
    ],
    timing_window_us: Annotated[
        int,
        typer.Option(help="Timing method: block-to-switch-in window in microseconds"),
    ] = DEFAULT_TIMING_WINDOW_US,
    timing_windows: Annotated[
        str | None,
        typer.Option(
            help=(
                "Comma-separated timing windows in microseconds (for example "
                "25,50,100,250,500,1000); emits one timing row per window and "
                "overrides --timing-window-us"
            ),
        ),
    ] = None,
    minimum_source_fraction: Annotated[
        float,
        typer.Option(
            help=(
                "Opt-in noise filter: minimum share of the source thread's "
                "outgoing evidence (default 0 = off)"
            ),
        ),
    ] = 0.0,
    csv_path: Annotated[
        Path | None,
        typer.Option("--csv", help="Write the per-run rows to this CSV file"),
    ] = None,
    dot_directory: Annotated[
        Path | None,
        typer.Option(
            help="Write one score-coloured DOT file per run and method into this directory",
        ),
    ] = None,
) -> int:
    """Run wakeup and timing on every manifest row and print a Markdown table."""
    if timing_windows is None:
        window_list = [timing_window_us]
    else:
        window_list = [int(part) for part in timing_windows.split(",") if part.strip()]
    results = run_comparison(
        read_manifest(str(manifest)), window_list, minimum_source_fraction
    )
    print(format_comparison_table(results))
    if csv_path is not None:
        write_results_csv(results, str(csv_path))
        print(f"\nwrote {csv_path}", file=sys.stderr)
    if dot_directory is not None:
        dot_directory.mkdir(parents=True, exist_ok=True)
        for result in results:
            trace_stem = Path(result.trace_path).name.removesuffix(".events.bin")
            dot_path = dot_directory / f"{result.workload}__{trace_stem}__{result.method_label}.dot"
            write_scored_graph_dot(result.graph, result.report, str(dot_path))
        print(f"wrote DOT files to {dot_directory}", file=sys.stderr)
    return 0


@app.command()
def plan(
    graph: Annotated[
        Path,
        typer.Argument(help="Wakeup graph.json produced by infer"),
    ],
    cpus: Annotated[
        int,
        typer.Option(help="Number of CPUs available for placement"),
    ],
) -> int:
    """Print a JSON thread-to-CPU plan: one disjoint CPU block per confident group."""
    placement_plan = build_plan(load_graph_json(str(graph)), cpus)
    print(json.dumps(placement_plan, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to ``infer`` or ``score``. Returns a process exit code."""
    try:
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exit_error:
        return int(exit_error.exit_code)
    return 0 if result is None else int(result)
