"""Relationship graph types, threshold labeling, and JSON / DOT output."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


DEFAULT_MINIMUM_COUNT = 5
DEFAULT_MINIMUM_FRACTION = 0.05


@dataclass
class InferenceSettings:
    """Thresholds and method-specific knobs shared by inference and output."""

    method: str
    minimum_count: int = DEFAULT_MINIMUM_COUNT
    minimum_fraction: float = DEFAULT_MINIMUM_FRACTION
    timing_window_us: int = 100


@dataclass
class DelaySummary:
    """Wakeup-to-execution delay percentiles in microseconds."""

    p50_us: float | None = None
    p95_us: float | None = None
    max_us: float | None = None
    sample_count: int = 0


@dataclass
class Edge:
    """One directed relationship between two threads."""

    source_thread_identifier: int
    destination_thread_identifier: int
    confidence: str = 'ambiguous'  # 'confident' or 'ambiguous'
    wakeup_count: int = 0
    reverse_wakeup_count: int = 0
    timing_count: int = 0
    delay: DelaySummary = field(default_factory=DelaySummary)

    def primary_count(self, method: str) -> int:
        if method == 'wakeup':
            return self.wakeup_count
        if method == 'timing':
            return self.timing_count
        return self.wakeup_count + self.timing_count


@dataclass
class ThreadSummary:
    """Per-thread evidence totals and delay summary."""

    thread_identifier: int
    incoming_evidence_count: int = 0
    outgoing_evidence_count: int = 0
    delay: DelaySummary = field(default_factory=DelaySummary)


@dataclass
class GraphMetadata:
    method: str
    minimum_count: int
    minimum_fraction: float
    timing_window_us: int | None
    trace_path: str
    target_process_identifier: int
    recorded_event_count: int | None
    dropped_event_count: int
    has_end_of_trace: bool
    outside_process_event_count: int


@dataclass
class Graph:
    metadata: GraphMetadata
    threads: dict[int, ThreadSummary] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def apply_thresholds(self, settings: InferenceSettings) -> None:
        """Label edges confident or ambiguous from count and incoming share."""
        incoming: dict[int, int] = {}
        for edge in self.edges:
            count = edge.primary_count(settings.method)
            incoming[edge.destination_thread_identifier] = (
                incoming.get(edge.destination_thread_identifier, 0) + count
            )
            if edge.source_thread_identifier not in self.threads:
                self.threads[edge.source_thread_identifier] = ThreadSummary(
                    edge.source_thread_identifier
                )
            if edge.destination_thread_identifier not in self.threads:
                self.threads[edge.destination_thread_identifier] = ThreadSummary(
                    edge.destination_thread_identifier
                )

        for edge in self.edges:
            count = edge.primary_count(settings.method)
            destination_total = incoming.get(edge.destination_thread_identifier, 0)
            fraction = (count / destination_total) if destination_total else 0.0
            if count >= settings.minimum_count and fraction >= settings.minimum_fraction:
                edge.confidence = 'confident'
            else:
                edge.confidence = 'ambiguous'

        for thread_summary in self.threads.values():
            thread_summary.incoming_evidence_count = 0
            thread_summary.outgoing_evidence_count = 0
        for edge in self.edges:
            count = edge.primary_count(settings.method)
            self.threads[edge.source_thread_identifier].outgoing_evidence_count += count
            self.threads[edge.destination_thread_identifier].incoming_evidence_count += count

    def loss_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.metadata.has_end_of_trace:
            warnings.append('trace has no END_OF_TRACE record; it may have been cut short')
        if self.metadata.dropped_event_count > 0:
            warnings.append(
                f'trace reports {self.metadata.dropped_event_count} dropped events'
            )
        return warnings


def summarize_delays_nanoseconds(delays_nanoseconds: list[int]) -> DelaySummary:
    """Compute p50, p95 and max from nanosecond samples; store microseconds."""
    if not delays_nanoseconds:
        return DelaySummary()
    ordered = sorted(delays_nanoseconds)
    count = len(ordered)

    def percentile(fraction: float) -> float:
        # Nearest-rank: ceil(fraction * n), clamped to [1, n].
        rank = max(1, min(count, int(fraction * count + 0.999999999)))
        return ordered[rank - 1] / 1000.0

    return DelaySummary(
        p50_us=percentile(0.50),
        p95_us=percentile(0.95),
        max_us=ordered[-1] / 1000.0,
        sample_count=count,
    )


def _delay_to_dict(delay: DelaySummary) -> dict[str, Any]:
    return {
        'p50_us': delay.p50_us,
        'p95_us': delay.p95_us,
        'max_us': delay.max_us,
        'sample_count': delay.sample_count,
    }


def graph_to_dict(graph: Graph) -> dict[str, Any]:
    metadata = asdict(graph.metadata)
    return {
        'metadata': metadata,
        'threads': {
            str(thread_identifier): {
                'thread_identifier': summary.thread_identifier,
                'incoming_evidence_count': summary.incoming_evidence_count,
                'outgoing_evidence_count': summary.outgoing_evidence_count,
                'delay': _delay_to_dict(summary.delay),
            }
            for thread_identifier, summary in sorted(graph.threads.items())
        },
        'edges': [
            {
                'source_thread_identifier': edge.source_thread_identifier,
                'destination_thread_identifier': edge.destination_thread_identifier,
                'confidence': edge.confidence,
                'wakeup_count': edge.wakeup_count,
                'reverse_wakeup_count': edge.reverse_wakeup_count,
                'timing_count': edge.timing_count,
                'delay': _delay_to_dict(edge.delay),
            }
            for edge in sorted(
                graph.edges,
                key=lambda edge: (
                    edge.source_thread_identifier,
                    edge.destination_thread_identifier,
                ),
            )
        ],
    }


def write_graph_json(graph: Graph, path: str) -> None:
    with open(path, 'w') as output_file:
        json.dump(graph_to_dict(graph), output_file, indent=2)
        output_file.write('\n')


def load_graph_json(path: str) -> Graph:
    with open(path) as input_file:
        data = json.load(input_file)
    metadata = GraphMetadata(**data['metadata'])
    threads: dict[int, ThreadSummary] = {}
    for thread_key, summary in data.get('threads', {}).items():
        delay = DelaySummary(**summary.get('delay', {}))
        threads[int(thread_key)] = ThreadSummary(
            thread_identifier=summary['thread_identifier'],
            incoming_evidence_count=summary.get('incoming_evidence_count', 0),
            outgoing_evidence_count=summary.get('outgoing_evidence_count', 0),
            delay=delay,
        )
    edges: list[Edge] = []
    for edge_data in data.get('edges', []):
        delay = DelaySummary(**edge_data.get('delay', {}))
        edges.append(
            Edge(
                source_thread_identifier=edge_data['source_thread_identifier'],
                destination_thread_identifier=edge_data['destination_thread_identifier'],
                confidence=edge_data.get('confidence', 'ambiguous'),
                wakeup_count=edge_data.get('wakeup_count', 0),
                reverse_wakeup_count=edge_data.get('reverse_wakeup_count', 0),
                timing_count=edge_data.get('timing_count', 0),
                delay=delay,
            )
        )
    return Graph(metadata=metadata, threads=threads, edges=edges)


def write_graph_dot(graph: Graph, path: str) -> None:
    """Write Graphviz DOT: solid confident edges, dashed ambiguous edges."""
    lines = ['digraph thread_relationships {', '  rankdir=LR;']
    for thread_identifier in sorted(graph.threads):
        lines.append(f'  t{thread_identifier} [label="{thread_identifier}"];')
    for edge in sorted(
        graph.edges,
        key=lambda edge: (edge.source_thread_identifier, edge.destination_thread_identifier),
    ):
        style = 'solid' if edge.confidence == 'confident' else 'dashed'
        if graph.metadata.method == 'timing':
            label_count = edge.timing_count
        else:
            label_count = edge.wakeup_count
        lines.append(
            f'  t{edge.source_thread_identifier} -> t{edge.destination_thread_identifier} '
            f'[style={style}, label="{label_count}"];'
        )
    lines.append('}')
    with open(path, 'w') as output_file:
        output_file.write('\n'.join(lines) + '\n')
