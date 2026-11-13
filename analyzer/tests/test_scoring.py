"""Scoring against ground-truth CSVs."""
from __future__ import annotations

from pathlib import Path

from analyzer.graph import (
    Edge,
    Graph,
    GraphMetadata,
    InferenceSettings,
    write_graph_json,
)
from analyzer.scoring import format_score_report, score_graph


def _empty_metadata(**overrides) -> GraphMetadata:
    values = dict(
        method='wakeup',
        minimum_count=5,
        minimum_fraction=0.05,
        timing_window_us=None,
        trace_path='test.jsonl',
        target_process_identifier=100,
        recorded_event_count=10,
        dropped_event_count=0,
        has_end_of_trace=True,
        outside_process_event_count=0,
    )
    values.update(overrides)
    return GraphMetadata(**values)


def _write_ground_truth(prefix: Path) -> None:
    threads_path = Path(f'{prefix}.threads.csv')
    handoffs_path = Path(f'{prefix}.handoffs.csv')
    threads_path.write_text(
        'tid,role,group,index\n'
        '1,main,-1,0\n'
        '10,producer,0,0\n'
        '11,worker,0,0\n'
        '12,sink,0,0\n'
        '20,producer,1,0\n'
        '21,worker,1,0\n'
        '22,sink,1,0\n'
    )
    handoffs_path.write_text(
        'group,item_id,from_tid,to_tid,stage,put_ns,get_ns\n'
        '0,0,10,11,producer_to_worker,100,200\n'
        '0,0,11,12,worker_to_sink,300,400\n'
        '1,0,20,21,producer_to_worker,100,200\n'
        '1,0,21,22,worker_to_sink,300,400\n'
    )


def test_perfect_score(tmp_path: Path) -> None:
    prefix = tmp_path / 'gt'
    _write_ground_truth(prefix)
    graph = Graph(
        metadata=_empty_metadata(),
        edges=[
            Edge(10, 11, confidence='confident', wakeup_count=10),
            Edge(11, 12, confidence='confident', wakeup_count=10),
            Edge(20, 21, confidence='confident', wakeup_count=10),
            Edge(21, 22, confidence='confident', wakeup_count=10),
            Edge(10, 12, confidence='ambiguous', wakeup_count=2),
        ],
    )
    report = score_graph(graph, str(prefix))
    assert report.directed_precision == 1.0
    assert report.directed_recall == 1.0
    assert report.undirected_precision == 1.0
    assert report.undirected_recall == 1.0
    assert report.cross_group_confident_edge_count == 0
    assert report.main_confident_edge_count == 0
    assert report.ambiguous_edge_count == 1
    assert report.missed_edges == []


def test_false_positives_and_misses(tmp_path: Path) -> None:
    prefix = tmp_path / 'gt'
    _write_ground_truth(prefix)
    graph = Graph(
        metadata=_empty_metadata(dropped_event_count=3, has_end_of_trace=False),
        edges=[
            Edge(10, 11, confidence='confident', wakeup_count=10),
            # Wrong direction.
            Edge(12, 11, confidence='confident', wakeup_count=8),
            # Cross-group false positive.
            Edge(10, 21, confidence='confident', wakeup_count=6),
            # Involving main.
            Edge(11, 1, confidence='confident', wakeup_count=5),
        ],
    )
    report = score_graph(graph, str(prefix))
    assert report.directed_true_positives == 1
    assert report.directed_false_positives == 3
    assert report.directed_false_negatives == 3
    assert report.cross_group_confident_edge_count == 1
    assert report.main_confident_edge_count == 1
    assert (11, 12) in report.missed_edges
    text = format_score_report(report)
    assert 'WARNING: trace has no END_OF_TRACE' in text
    assert 'dropped events' in text


def test_round_trip_json(tmp_path: Path) -> None:
    from analyzer.graph import load_graph_json

    graph = Graph(
        metadata=_empty_metadata(),
        edges=[Edge(1, 2, confidence='confident', wakeup_count=9, reverse_wakeup_count=1)],
    )
    path = tmp_path / 'graph.json'
    write_graph_json(graph, str(path))
    loaded = load_graph_json(str(path))
    assert loaded.edges[0].wakeup_count == 9
    assert loaded.edges[0].reverse_wakeup_count == 1
    assert loaded.metadata.method == 'wakeup'
