"""Score an inferred graph against the benchmark ground-truth CSVs.

Ground truth (see ``docs/ground-truth-format.md``):

- ``PREFIX.threads.csv`` — which tid is producer / worker / sink / main, and group.
- ``PREFIX.handoffs.csv`` — one row per queue handoff; true edges are the distinct
  ``(from_tid, to_tid)`` pairs.

Only **confident** edges are scored. Ambiguous edges are counted but do not
affect precision or recall.

Headline metric: **directed** precision / recall. Undirected figures treat
A→B and B→A as the same edge (useful when reverse wakeups flipped a pair).
Cross-group and ``main`` confident edges are reported separately: independent
pipeline groups must not be linked, and join wakeups into ``main`` are noise.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field

from analyzer.graph import Graph, write_graph_dot


@dataclass
class ThreadInfo:
    """One row from ``PREFIX.threads.csv`` (benchmark role metadata)."""

    thread_id: int
    role: str
    group: int
    index: int


@dataclass
class ScoreReport:
    """All numbers printed by ``analyzer score`` (and useful for tests)."""

    true_edge_count: int
    confident_edge_count: int
    ambiguous_edge_count: int
    directed_true_positives: int
    directed_false_positives: int
    directed_false_negatives: int
    directed_precision: float | None
    directed_recall: float | None
    undirected_true_positives: int
    undirected_false_positives: int
    undirected_false_negatives: int
    undirected_precision: float | None
    undirected_recall: float | None
    # Should be 0 on a correct run of the multi-group benchmark.
    cross_group_confident_edge_count: int
    # Usually join/teardown noise if non-zero and confident.
    main_confident_edge_count: int
    missed_edges: list[tuple[int, int]] = field(default_factory=list)
    true_positive_edges: list[tuple[int, int]] = field(default_factory=list)
    false_positive_edges: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    threads: dict[int, ThreadInfo] = field(default_factory=dict)


def load_ground_truth(
    prefix: str,
) -> tuple[dict[int, ThreadInfo], set[tuple[int, int]]]:
    """Read ``PREFIX.threads.csv`` and ``PREFIX.handoffs.csv``.

    True edges are the set of distinct handoff pairs — item counts are ignored
    for precision/recall (edge-set scoring, not weighted by volume).
    """
    threads: dict[int, ThreadInfo] = {}
    with open(f"{prefix}.threads.csv", newline="") as threads_file:
        for row in csv.DictReader(threads_file):
            # Column name "tid" is the benchmark's spelling of thread id (gettid()).
            thread_id = int(row["tid"])
            threads[thread_id] = ThreadInfo(
                thread_id=thread_id,
                role=row["role"],
                group=int(row["group"]),
                index=int(row["index"]),
            )
    true_edges: set[tuple[int, int]] = set()
    with open(f"{prefix}.handoffs.csv", newline="") as handoffs_file:
        for row in csv.DictReader(handoffs_file):
            true_edges.add((int(row["from_tid"]), int(row["to_tid"])))
    return threads, true_edges


def _ratio(numerator: int, denominator: int) -> float | None:
    """Return numerator/denominator, or None when the denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator


def score_graph(graph: Graph, ground_truth_prefix: str) -> ScoreReport:
    """Compare confident edges in the graph to the true edge set."""
    threads, true_edges = load_ground_truth(ground_truth_prefix)
    confident = {
        (edge.source_thread_id, edge.destination_thread_id)
        for edge in graph.edges
        if edge.confidence == "confident"
    }
    ambiguous_count = sum(1 for edge in graph.edges if edge.confidence == "ambiguous")

    directed_true_positives = len(confident & true_edges)
    directed_false_positives = len(confident - true_edges)
    directed_false_negatives = len(true_edges - confident)

    # Undirected: direction mistakes still count as a recovered relationship.
    true_undirected = {frozenset(edge) for edge in true_edges}
    confident_undirected = {frozenset(edge) for edge in confident}
    undirected_true_positives = len(confident_undirected & true_undirected)
    undirected_false_positives = len(confident_undirected - true_undirected)
    undirected_false_negatives = len(true_undirected - confident_undirected)

    cross_group = 0
    involving_main = 0
    for source, destination in confident:
        source_info = threads.get(source)
        destination_info = threads.get(destination)
        if source_info is None or destination_info is None:
            continue
        if source_info.role == "main" or destination_info.role == "main":
            involving_main += 1
        elif source_info.group != destination_info.group:
            # Independent pipelines must not exchange work.
            cross_group += 1

    return ScoreReport(
        true_edge_count=len(true_edges),
        confident_edge_count=len(confident),
        ambiguous_edge_count=ambiguous_count,
        directed_true_positives=directed_true_positives,
        directed_false_positives=directed_false_positives,
        directed_false_negatives=directed_false_negatives,
        directed_precision=_ratio(directed_true_positives, len(confident)),
        directed_recall=_ratio(directed_true_positives, len(true_edges)),
        undirected_true_positives=undirected_true_positives,
        undirected_false_positives=undirected_false_positives,
        undirected_false_negatives=undirected_false_negatives,
        undirected_precision=_ratio(
            undirected_true_positives, len(confident_undirected)
        ),
        undirected_recall=_ratio(undirected_true_positives, len(true_undirected)),
        cross_group_confident_edge_count=cross_group,
        main_confident_edge_count=involving_main,
        missed_edges=sorted(true_edges - confident),
        true_positive_edges=sorted(confident & true_edges),
        false_positive_edges=sorted(confident - true_edges),
        warnings=graph.loss_warnings(),
        threads=threads,
    )


def write_scored_graph_dot(graph: Graph, report: ScoreReport, path: str) -> None:
    """Write the DOT figure coloured by the same ``ScoreReport`` the tables use."""
    write_graph_dot(
        graph,
        path,
        true_positive_edges=set(report.true_positive_edges),
        false_positive_edges=set(report.false_positive_edges),
        missed_edges=set(report.missed_edges),
    )


def format_thread(thread_id: int, threads: dict[int, ThreadInfo]) -> str:
    """Pretty-print a tid using benchmark role metadata when available."""
    info = threads.get(thread_id)
    if info is None:
        return f"unknown({thread_id})"
    return f"{info.role}[g{info.group}.{info.index}]({thread_id})"


def format_score_report(report: ScoreReport) -> str:
    """Format a ``ScoreReport`` as the multi-line text printed by ``analyzer score``."""
    lines: list[str] = []
    for warning in report.warnings:
        lines.append(f"WARNING: {warning}")
    lines.append(
        f"true edges: {report.true_edge_count}  "
        f"confident: {report.confident_edge_count}  "
        f"ambiguous: {report.ambiguous_edge_count}"
    )

    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    lines.append(
        f"directed:   precision={fmt(report.directed_precision)}  "
        f"recall={fmt(report.directed_recall)}  "
        f"tp={report.directed_true_positives}  "
        f"fp={report.directed_false_positives}  "
        f"fn={report.directed_false_negatives}"
    )
    lines.append(
        f"undirected: precision={fmt(report.undirected_precision)}  "
        f"recall={fmt(report.undirected_recall)}  "
        f"tp={report.undirected_true_positives}  "
        f"fp={report.undirected_false_positives}  "
        f"fn={report.undirected_false_negatives}"
    )
    lines.append(
        f"cross-group confident edges: {report.cross_group_confident_edge_count}  "
        f"main confident edges: {report.main_confident_edge_count}"
    )
    if report.missed_edges:
        lines.append("missed true edges:")
        for source, destination in report.missed_edges:
            lines.append(
                f"  {format_thread(source, report.threads)} -> "
                f"{format_thread(destination, report.threads)}"
            )
    if report.false_positive_edges:
        lines.append("false positive edges:")
        for source, destination in report.false_positive_edges:
            lines.append(
                f"  {format_thread(source, report.threads)} -> "
                f"{format_thread(destination, report.threads)}"
            )
    return "\n".join(lines)
