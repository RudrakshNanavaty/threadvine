"""Score an inferred graph against the benchmark ground-truth CSVs."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field

from analyzer.graph import Graph


@dataclass
class ThreadInfo:
    thread_identifier: int
    role: str
    group: int
    index: int


@dataclass
class ScoreReport:
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
    cross_group_confident_edge_count: int
    main_confident_edge_count: int
    missed_edges: list[tuple[int, int]] = field(default_factory=list)
    false_positive_edges: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    threads: dict[int, ThreadInfo] = field(default_factory=dict)


def load_ground_truth(prefix: str) -> tuple[dict[int, ThreadInfo], set[tuple[int, int]]]:
    """Read PREFIX.threads.csv and PREFIX.handoffs.csv into threads and true edges."""
    threads: dict[int, ThreadInfo] = {}
    with open(f'{prefix}.threads.csv', newline='') as threads_file:
        for row in csv.DictReader(threads_file):
            thread_identifier = int(row['tid'])
            threads[thread_identifier] = ThreadInfo(
                thread_identifier=thread_identifier,
                role=row['role'],
                group=int(row['group']),
                index=int(row['index']),
            )
    true_edges: set[tuple[int, int]] = set()
    with open(f'{prefix}.handoffs.csv', newline='') as handoffs_file:
        for row in csv.DictReader(handoffs_file):
            true_edges.add((int(row['from_tid']), int(row['to_tid'])))
    return threads, true_edges


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def score_graph(graph: Graph, ground_truth_prefix: str) -> ScoreReport:
    """Compare confident edges in the graph to the true edge set."""
    threads, true_edges = load_ground_truth(ground_truth_prefix)
    confident = {
        (edge.source_thread_identifier, edge.destination_thread_identifier)
        for edge in graph.edges
        if edge.confidence == 'confident'
    }
    ambiguous_count = sum(1 for edge in graph.edges if edge.confidence == 'ambiguous')

    directed_true_positives = len(confident & true_edges)
    directed_false_positives = len(confident - true_edges)
    directed_false_negatives = len(true_edges - confident)

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
        if source_info.role == 'main' or destination_info.role == 'main':
            involving_main += 1
        elif source_info.group != destination_info.group:
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
        undirected_precision=_ratio(undirected_true_positives, len(confident_undirected)),
        undirected_recall=_ratio(undirected_true_positives, len(true_undirected)),
        cross_group_confident_edge_count=cross_group,
        main_confident_edge_count=involving_main,
        missed_edges=sorted(true_edges - confident),
        false_positive_edges=sorted(confident - true_edges),
        warnings=graph.loss_warnings(),
        threads=threads,
    )


def format_thread(thread_identifier: int, threads: dict[int, ThreadInfo]) -> str:
    info = threads.get(thread_identifier)
    if info is None:
        return f'unknown({thread_identifier})'
    return f'{info.role}[g{info.group}.{info.index}]({thread_identifier})'


def format_score_report(report: ScoreReport) -> str:
    lines: list[str] = []
    for warning in report.warnings:
        lines.append(f'WARNING: {warning}')
    lines.append(
        f'true edges: {report.true_edge_count}  '
        f'confident: {report.confident_edge_count}  '
        f'ambiguous: {report.ambiguous_edge_count}'
    )

    def fmt(value: float | None) -> str:
        return 'n/a' if value is None else f'{value:.3f}'

    lines.append(
        f'directed:   precision={fmt(report.directed_precision)}  '
        f'recall={fmt(report.directed_recall)}  '
        f'tp={report.directed_true_positives}  '
        f'fp={report.directed_false_positives}  '
        f'fn={report.directed_false_negatives}'
    )
    lines.append(
        f'undirected: precision={fmt(report.undirected_precision)}  '
        f'recall={fmt(report.undirected_recall)}  '
        f'tp={report.undirected_true_positives}  '
        f'fp={report.undirected_false_positives}  '
        f'fn={report.undirected_false_negatives}'
    )
    lines.append(
        f'cross-group confident edges: {report.cross_group_confident_edge_count}  '
        f'main confident edges: {report.main_confident_edge_count}'
    )
    if report.missed_edges:
        lines.append('missed true edges:')
        for source, destination in report.missed_edges:
            lines.append(
                f'  {format_thread(source, report.threads)} -> '
                f'{format_thread(destination, report.threads)}'
            )
    if report.false_positive_edges:
        lines.append('false positive edges:')
        for source, destination in report.false_positive_edges:
            lines.append(
                f'  {format_thread(source, report.threads)} -> '
                f'{format_thread(destination, report.threads)}'
            )
    return '\n'.join(lines)
