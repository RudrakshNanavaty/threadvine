"""First-wakeup orientation follows the flow under backpressure and ignores tiny blips."""
from __future__ import annotations

from pathlib import Path

from analyzer import wakeup_inference
from analyzer.graph import InferenceSettings
from analyzer.trace import prepare_trace

from conftest import end_of_trace, waking, write_jsonl


def _edge_pair(
    tmp_path: Path,
    early_reverse_count: int,
    backpressure_count: int,
    orientation: str,
) -> tuple[int, int]:
    """Producer is thread 10, worker is thread 11; returns the single edge's (source, destination)."""
    events = []
    time = 1_000_000
    for _ in range(early_reverse_count):
        events.append(waking(time, waker=11, woken=10))
        time += 1000
    for _ in range(8):  # real flow: producer -> worker
        events.append(waking(time, waker=10, woken=11))
        time += 1000
    for _ in range(backpressure_count):  # full queue: worker -> producer
        events.append(waking(time, waker=11, woken=10))
        time += 1000
    events.append(end_of_trace(len(events)))
    trace_path = tmp_path / 'pair.jsonl'
    write_jsonl(trace_path, events)
    graph = wakeup_inference.infer(
        prepare_trace(str(trace_path)),
        InferenceSettings(method='wakeup', orientation=orientation),
    )
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    return edge.source_thread_id, edge.destination_thread_id


def test_first_wakeup_follows_the_flow_when_backpressure_dominates(tmp_path: Path) -> None:
    # 8 flow wakeups come first, then 100 backpressure wakeups the other way.
    assert _edge_pair(tmp_path, 0, 100, 'volume') == (11, 10)
    assert _edge_pair(tmp_path, 0, 100, 'first-wakeup') == (10, 11)


def test_first_wakeup_ignores_a_tiny_early_reverse_blip(tmp_path: Path) -> None:
    # Two stray reverse wakeups before the flow are below minimum_count, so they cannot flip it.
    assert _edge_pair(tmp_path, 2, 0, 'first-wakeup') == (10, 11)
