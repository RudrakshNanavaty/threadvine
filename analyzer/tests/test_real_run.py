"""End-to-end test on a committed short benchmark + collector capture."""
from __future__ import annotations

from pathlib import Path

from analyzer.cli import main
from analyzer.graph import load_graph_json
from analyzer.scoring import score_graph

FIXTURE_DIRECTORY = Path(__file__).parent / 'fixtures' / 'real_run'


def test_wakeup_scores_perfectly_on_real_run(tmp_path: Path) -> None:
    trace_path = FIXTURE_DIRECTORY / 'traced_run.events.bin'
    groundtruth_prefix = str(FIXTURE_DIRECTORY / 'groundtruth')
    graph_path = tmp_path / 'graph.json'

    assert main([
        'infer', str(trace_path),
        '--method', 'wakeup',
        '-o', str(graph_path),
    ]) == 0

    report = score_graph(load_graph_json(str(graph_path)), groundtruth_prefix)
    assert report.directed_precision == 1.0
    assert report.directed_recall == 1.0
    assert report.cross_group_confident_edge_count == 0
    assert report.main_confident_edge_count == 0
    # Startup / teardown wakeups to main stay below the confidence threshold.
    assert report.ambiguous_edge_count >= 1
