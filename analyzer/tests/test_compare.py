"""Smoke test: ``compare`` agrees with ``score_graph``."""
from __future__ import annotations

from pathlib import Path

from analyzer import wakeup_inference
from analyzer.compare import read_manifest, run_comparison
from analyzer.graph import InferenceSettings
from analyzer.scoring import score_graph
from analyzer.trace import prepare_trace

FIXTURE_DIRECTORY = Path(__file__).parent / 'fixtures' / 'real_run'


def test_compare_matches_score_graph_on_real_run(tmp_path: Path) -> None:
    manifest_path = tmp_path / 'manifest.csv'
    manifest_path.write_text(
        'workload,trace_path,groundtruth_prefix\n'
        f'real_run,{FIXTURE_DIRECTORY / "traced_run.events.bin"},'
        f'{FIXTURE_DIRECTORY / "groundtruth"}\n'
    )

    results = run_comparison(read_manifest(str(manifest_path)), [100])

    by_label = {result.method_label: result for result in results}
    assert set(by_label) == {'wakeup', 'timing@100us'}

    expected_graph = wakeup_inference.infer(
        prepare_trace(str(FIXTURE_DIRECTORY / 'traced_run.events.bin')),
        InferenceSettings(method='wakeup'),
    )
    expected = score_graph(expected_graph, str(FIXTURE_DIRECTORY / 'groundtruth'))
    actual = by_label['wakeup'].report
    assert actual == expected
    assert actual.directed_precision == 1.0
    assert actual.directed_recall == 1.0
