"""Compare the wakeup and timing methods across many runs.

A manifest CSV lists one row per captured run::

    workload,trace_path,groundtruth_prefix
    calm,traces/calm_1.events.bin,traces/calm_1
    calm,traces/calm_2.events.bin,traces/calm_2
    spin,traces/spin_1.events.bin,traces/spin_1

Rows that share a workload name are repeats of the same workload. Relative
paths are resolved against the manifest's own directory.

Every run goes through the same pipeline the ``infer`` and ``score`` commands
use (``prepare_trace`` -> ``infer`` -> ``score_graph``) with identical default
thresholds, so the table compares methods and not tuning.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from analyzer import timing_inference, wakeup_inference
from analyzer.graph import Graph, InferenceSettings
from analyzer.scoring import ScoreReport, score_graph
from analyzer.trace import prepare_trace

DEFAULT_TIMING_WINDOW_US = 100

CSV_COLUMNS = [
    "workload",
    "trace_path",
    "method",
    "timing_window_us",
    "true_edges",
    "confident_edges",
    "ambiguous_edges",
    "directed_precision",
    "directed_recall",
    "false_positives",
    "missed_edges",
    "cross_group_confident_edges",
    "main_confident_edges",
]


@dataclass
class ManifestRow:
    """One captured run: its workload name, trace file and ground-truth prefix."""

    workload: str
    trace_path: str
    groundtruth_prefix: str


@dataclass
class RunResult:
    """Score of one inference method on one run."""

    workload: str
    trace_path: str
    # "wakeup", or "timing@<window>" for the timing method at that window.
    method_label: str
    timing_window_us: int | None
    graph: Graph
    report: ScoreReport


def read_manifest(manifest_path: str) -> list[ManifestRow]:
    """Read the manifest CSV, resolving relative paths against its directory."""
    base_directory = Path(manifest_path).parent
    rows: list[ManifestRow] = []
    with open(manifest_path, newline="") as manifest_file:
        for row in csv.DictReader(manifest_file):
            rows.append(
                ManifestRow(
                    workload=row["workload"],
                    trace_path=str(base_directory / row["trace_path"]),
                    groundtruth_prefix=str(base_directory / row["groundtruth_prefix"]),
                )
            )
    return rows


def method_label(method: str, timing_window_us: int | None) -> str:
    """Name used in tables and CSV rows for one method configuration."""
    if method == "timing":
        return f"timing@{timing_window_us}us"
    return method


def run_comparison(
    rows: list[ManifestRow],
    timing_windows_us: list[int],
    minimum_source_fraction: float = 0.0,
    orientation: str = "volume",
) -> list[RunResult]:
    """Score wakeup plus the timing method at each window, for every manifest row."""
    results: list[RunResult] = []
    for row in rows:
        loaded_trace = prepare_trace(row.trace_path)

        wakeup_settings = InferenceSettings(
            method="wakeup",
            minimum_source_fraction=minimum_source_fraction,
            orientation=orientation,
        )
        wakeup_graph = wakeup_inference.infer(loaded_trace, wakeup_settings)
        results.append(
            RunResult(
                workload=row.workload,
                trace_path=row.trace_path,
                method_label=method_label("wakeup", None),
                timing_window_us=None,
                graph=wakeup_graph,
                report=score_graph(wakeup_graph, row.groundtruth_prefix),
            )
        )

        for timing_window_us in timing_windows_us:
            timing_settings = InferenceSettings(
                method="timing",
                timing_window_us=timing_window_us,
                minimum_source_fraction=minimum_source_fraction,
            )
            timing_graph = timing_inference.infer(loaded_trace, timing_settings)
            results.append(
                RunResult(
                    workload=row.workload,
                    trace_path=row.trace_path,
                    method_label=method_label("timing", timing_window_us),
                    timing_window_us=timing_window_us,
                    graph=timing_graph,
                    report=score_graph(timing_graph, row.groundtruth_prefix),
                )
            )
    return results


def _csv_ratio(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def write_results_csv(results: list[RunResult], path: str) -> None:
    """Write one row per (run, method) with the raw per-run numbers."""
    with open(path, "w", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(CSV_COLUMNS)
        for result in results:
            report = result.report
            writer.writerow(
                [
                    result.workload,
                    result.trace_path,
                    result.method_label,
                    "" if result.timing_window_us is None else result.timing_window_us,
                    report.true_edge_count,
                    report.confident_edge_count,
                    report.ambiguous_edge_count,
                    _csv_ratio(report.directed_precision),
                    _csv_ratio(report.directed_recall),
                    report.directed_false_positives,
                    report.directed_false_negatives,
                    report.cross_group_confident_edge_count,
                    report.main_confident_edge_count,
                ]
            )


def _summarize(values: list[float | int | None], is_ratio: bool) -> str:
    """Format ``mean (min-max)`` over repeats; a single repeat prints one number."""
    present = [value for value in values if value is not None]
    if not present:
        return "n/a"

    def fmt(value: float) -> str:
        return f"{value:.3f}" if is_ratio else f"{value:g}"

    mean = sum(present) / len(present)
    low, high = min(present), max(present)
    if len(values) == 1:
        return fmt(mean)
    if low == high:
        return fmt(mean)
    return f"{fmt(mean)} ({fmt(low)}-{fmt(high)})"


def format_comparison_table(results: list[RunResult]) -> str:
    """Markdown table: one row per (workload, method), summarized over repeats."""
    grouped: dict[tuple[str, str], list[RunResult]] = {}
    for result in results:
        grouped.setdefault((result.workload, result.method_label), []).append(result)

    header = [
        "workload",
        "method",
        "runs",
        "directed precision",
        "directed recall",
        "false positives",
        "missed edges",
        "cross-group confident",
        "ambiguous",
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for (workload, label), group in grouped.items():
        reports = [result.report for result in group]
        cells = [
            workload,
            label,
            str(len(group)),
            _summarize([report.directed_precision for report in reports], True),
            _summarize([report.directed_recall for report in reports], True),
            _summarize([report.directed_false_positives for report in reports], False),
            _summarize([report.directed_false_negatives for report in reports], False),
            _summarize(
                [report.cross_group_confident_edge_count for report in reports], False
            ),
            _summarize([report.ambiguous_edge_count for report in reports], False),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)

