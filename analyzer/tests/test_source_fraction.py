"""Opt-in minimum source fraction drops tiny-share edges and is off by default."""
from __future__ import annotations

from pathlib import Path

from analyzer.compare import ManifestRow, run_comparison
from analyzer.graph import Edge, Graph, GraphMetadata, InferenceSettings

FIXTURE_DIRECTORY = Path(__file__).parent / 'fixtures' / 'real_run'


def _graph_with_a_noise_edge() -> Graph:
    metadata = GraphMetadata(
        method='wakeup',
        minimum_count=5,
        minimum_fraction=0.05,
        timing_window_us=None,
        trace_path='test.jsonl',
        target_pid=100,
        recorded_event_count=0,
        dropped_event_count=0,
        has_end_of_trace=True,
        outside_process_event_count=0,
    )
    return Graph(
        metadata=metadata,
        edges=[
            # Thread 10 sends 1000 wakeups to 11 and only 20 to 12 (about 2% of its traffic).
            Edge(10, 11, wakeup_count=1000),
            Edge(10, 12, wakeup_count=20),
        ],
    )


def test_noise_edge_is_confident_by_default_and_dropped_with_the_flag() -> None:
    default_graph = _graph_with_a_noise_edge()
    default_graph.apply_thresholds(InferenceSettings(method='wakeup'))
    assert [edge.confidence for edge in default_graph.edges] == ['confident', 'confident']

    filtered_graph = _graph_with_a_noise_edge()
    filtered_graph.apply_thresholds(
        InferenceSettings(method='wakeup', minimum_source_fraction=0.05)
    )
    assert [edge.confidence for edge in filtered_graph.edges] == ['confident', 'ambiguous']


def test_flag_keeps_the_real_run_fixture_perfect() -> None:
    row = ManifestRow(
        workload='real_run',
        trace_path=str(FIXTURE_DIRECTORY / 'traced_run.events.bin'),
        groundtruth_prefix=str(FIXTURE_DIRECTORY / 'groundtruth'),
    )
    results = run_comparison([row], [100], minimum_source_fraction=0.01)
    wakeup_report = next(r for r in results if r.method_label == 'wakeup').report
    assert wakeup_report.directed_precision == 1.0
    assert wakeup_report.directed_recall == 1.0
