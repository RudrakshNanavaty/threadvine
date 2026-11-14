"""Relationship graph types, confidence thresholds, and JSON / DOT output.

An edge A → B means "A hands work to B" (same direction as the benchmark
ground truth). Each edge carries evidence counts by kind; milestone 1 uses
``wakeup_count`` / ``reverse_wakeup_count`` or ``timing_count``.

Confidence (frozen defaults after calibration — see ``analyzer/README.md``):

- **confident** if ``primary_count >= minimum_count`` (default 5) *and* the edge
  is at least ``minimum_fraction`` (default 0.05) of the destination thread's
  incoming evidence.
- **ambiguous** otherwise: listed in the graph and drawn dashed in DOT, but
  excluded from precision / recall.

Startup ``info_lock`` chatter and ``pthread_join`` wakeups into ``main`` usually
fall below the count threshold and stay ambiguous without special-case trimming.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Frozen after the -g 2 -w 2 calibration run; override only for experiments.
DEFAULT_MINIMUM_COUNT = 5
DEFAULT_MINIMUM_FRACTION = 0.05


@dataclass
class InferenceSettings:
    """Thresholds and method-specific knobs shared by inference and output."""

    method: str
    minimum_count: int = DEFAULT_MINIMUM_COUNT
    minimum_fraction: float = DEFAULT_MINIMUM_FRACTION
    # Used only by the timing method (microseconds).
    timing_window_us: int = 100


@dataclass
class DelaySummary:
    """Wakeup-to-execution delay percentiles in microseconds.

    Delay = time from ``sched_waking`` until the woken thread's next
    ``sched_switch`` switch-in. Useful for studying scheduling latency.
    """

    p50_us: float | None = None
    p95_us: float | None = None
    max_us: float | None = None
    sample_count: int = 0


@dataclass
class Edge:
    """One directed relationship between two threads."""

    source_thread_id: int
    destination_thread_id: int
    confidence: str = "ambiguous"  # 'confident' or 'ambiguous'
    # Wakeups in the edge direction after dominant-direction collapse.
    wakeup_count: int = 0
    # Wakeups the other way (e.g. full-queue backpressure); kept for diagnosis.
    reverse_wakeup_count: int = 0
    timing_count: int = 0
    delay: DelaySummary = field(default_factory=DelaySummary)

    def primary_count(self, method: str) -> int:
        """Evidence count used for thresholding under the given method."""
        if method == "wakeup":
            return self.wakeup_count
        if method == "timing":
            return self.timing_count
        # Reserved for a future combined method.
        return self.wakeup_count + self.timing_count


@dataclass
class ThreadSummary:
    """Per-thread evidence totals and (for wakeup) delay summary."""

    thread_id: int
    incoming_evidence_count: int = 0
    outgoing_evidence_count: int = 0
    delay: DelaySummary = field(default_factory=DelaySummary)


@dataclass
class GraphMetadata:
    """Provenance and loss flags stored next to the edges in ``graph.json``."""

    method: str
    minimum_count: int
    minimum_fraction: float
    timing_window_us: int | None
    trace_path: str
    target_pid: int
    recorded_event_count: int | None
    dropped_event_count: int
    has_end_of_trace: bool
    outside_process_event_count: int


@dataclass
class Graph:
    """Inferred relationship graph: metadata, per-thread summaries, and edges."""

    metadata: GraphMetadata
    threads: dict[int, ThreadSummary] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def apply_thresholds(self, settings: InferenceSettings) -> None:
        """Label edges confident or ambiguous; refresh per-thread evidence totals."""
        # First pass: total incoming evidence per destination (denominator for fraction).
        incoming: dict[int, int] = {}
        for edge in self.edges:
            count = edge.primary_count(settings.method)
            incoming[edge.destination_thread_id] = (
                incoming.get(edge.destination_thread_id, 0) + count
            )
            if edge.source_thread_id not in self.threads:
                self.threads[edge.source_thread_id] = ThreadSummary(
                    edge.source_thread_id
                )
            if edge.destination_thread_id not in self.threads:
                self.threads[edge.destination_thread_id] = ThreadSummary(
                    edge.destination_thread_id
                )

        for edge in self.edges:
            count = edge.primary_count(settings.method)
            destination_total = incoming.get(edge.destination_thread_id, 0)
            fraction = (count / destination_total) if destination_total else 0.0
            if (
                count >= settings.minimum_count
                and fraction >= settings.minimum_fraction
            ):
                edge.confidence = "confident"
            else:
                edge.confidence = "ambiguous"

        for thread_summary in self.threads.values():
            thread_summary.incoming_evidence_count = 0
            thread_summary.outgoing_evidence_count = 0
        for edge in self.edges:
            count = edge.primary_count(settings.method)
            self.threads[edge.source_thread_id].outgoing_evidence_count += count
            self.threads[
                edge.destination_thread_id
            ].incoming_evidence_count += count

    def loss_warnings(self) -> list[str]:
        """Human-readable warnings about incomplete or lossy traces."""
        warnings: list[str] = []
        if not self.metadata.has_end_of_trace:
            warnings.append(
                "trace has no END_OF_TRACE record; it may have been cut short"
            )
        if self.metadata.dropped_event_count > 0:
            warnings.append(
                f"trace reports {self.metadata.dropped_event_count} dropped events"
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
        "p50_us": delay.p50_us,
        "p95_us": delay.p95_us,
        "max_us": delay.max_us,
        "sample_count": delay.sample_count,
    }


def graph_to_dict(graph: Graph) -> dict[str, Any]:
    """Serialize a graph to the ``graph.json`` dictionary shape."""
    metadata = asdict(graph.metadata)
    return {
        "metadata": metadata,
        "threads": {
            str(thread_id): {
                "thread_id": summary.thread_id,
                "incoming_evidence_count": summary.incoming_evidence_count,
                "outgoing_evidence_count": summary.outgoing_evidence_count,
                "delay": _delay_to_dict(summary.delay),
            }
            for thread_id, summary in sorted(graph.threads.items())
        },
        "edges": [
            {
                "source_thread_id": edge.source_thread_id,
                "destination_thread_id": edge.destination_thread_id,
                "confidence": edge.confidence,
                "wakeup_count": edge.wakeup_count,
                "reverse_wakeup_count": edge.reverse_wakeup_count,
                "timing_count": edge.timing_count,
                "delay": _delay_to_dict(edge.delay),
            }
            for edge in sorted(
                graph.edges,
                key=lambda edge: (
                    edge.source_thread_id,
                    edge.destination_thread_id,
                ),
            )
        ],
    }


def write_graph_json(graph: Graph, path: str) -> None:
    """Write ``graph.json`` (indent=2) for ``analyzer score`` and humans."""
    with open(path, "w") as output_file:
        json.dump(graph_to_dict(graph), output_file, indent=2)
        output_file.write("\n")


def load_graph_json(path: str) -> Graph:
    """Load a graph previously written by ``write_graph_json``."""
    with open(path) as input_file:
        data = json.load(input_file)
    metadata = GraphMetadata(**data["metadata"])
    threads: dict[int, ThreadSummary] = {}
    for thread_key, summary in data.get("threads", {}).items():
        delay = DelaySummary(**summary.get("delay", {}))
        threads[int(thread_key)] = ThreadSummary(
            thread_id=summary["thread_id"],
            incoming_evidence_count=summary.get("incoming_evidence_count", 0),
            outgoing_evidence_count=summary.get("outgoing_evidence_count", 0),
            delay=delay,
        )
    edges: list[Edge] = []
    for edge_data in data.get("edges", []):
        delay = DelaySummary(**edge_data.get("delay", {}))
        edges.append(
            Edge(
                source_thread_id=edge_data["source_thread_id"],
                destination_thread_id=edge_data[
                    "destination_thread_id"
                ],
                confidence=edge_data.get("confidence", "ambiguous"),
                wakeup_count=edge_data.get("wakeup_count", 0),
                reverse_wakeup_count=edge_data.get("reverse_wakeup_count", 0),
                timing_count=edge_data.get("timing_count", 0),
                delay=delay,
            )
        )
    return Graph(metadata=metadata, threads=threads, edges=edges)


def write_graph_dot(
    graph: Graph,
    path: str,
    true_positive_edges: set[tuple[int, int]] | None = None,
    false_positive_edges: set[tuple[int, int]] | None = None,
    missed_edges: set[tuple[int, int]] | None = None,
) -> None:
    """Write Graphviz DOT: solid confident edges, dashed ambiguous edges.

    When the three score sets are given (taken from ``ScoreReport``), confident
    edges are coloured: true positive green, false positive red. True edges the
    graph missed are added as dashed grey edges. Without them the output is the
    plain uncoloured graph.

    Render with e.g. ``dot -Tpng graph.dot -o graph.png``.
    """
    colour_by_score = (
        true_positive_edges is not None
        or false_positive_edges is not None
        or missed_edges is not None
    )
    true_positive_edges = true_positive_edges or set()
    false_positive_edges = false_positive_edges or set()
    missed_edges = missed_edges or set()

    thread_ids = set(graph.threads)
    for source, destination in missed_edges:
        thread_ids.update((source, destination))

    lines = ["digraph thread_relationships {", "  rankdir=LR;"]
    for thread_id in sorted(thread_ids):
        lines.append(f'  t{thread_id} [label="{thread_id}"];')
    for edge in sorted(
        graph.edges,
        key=lambda edge: (
            edge.source_thread_id,
            edge.destination_thread_id,
        ),
    ):
        style = "solid" if edge.confidence == "confident" else "dashed"
        if graph.metadata.method == "timing":
            label_count = edge.timing_count
        else:
            label_count = edge.wakeup_count
        edge_pair = (edge.source_thread_id, edge.destination_thread_id)
        colour = ""
        if colour_by_score:
            if edge_pair in true_positive_edges:
                colour = ', color="green3", fontcolor="green3"'
            elif edge_pair in false_positive_edges:
                colour = ', color="red", fontcolor="red"'
        lines.append(
            f"  t{edge.source_thread_id} -> t{edge.destination_thread_id} "
            f'[style={style}, label="{label_count}"{colour}];'
        )
    for source, destination in sorted(missed_edges):
        lines.append(
            f"  t{source} -> t{destination} "
            '[style=dashed, color="grey", fontcolor="grey", label="missed"];'
        )
    lines.append("}")
    with open(path, "w") as output_file:
        output_file.write("\n".join(lines) + "\n")
