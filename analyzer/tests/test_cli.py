"""End-to-end CLI: infer then score on a small synthetic pipeline."""
from __future__ import annotations

from pathlib import Path

from analyzer.cli import main

from conftest import end_of_trace, waking, write_jsonl


def test_infer_and_score_round_trip(tmp_path: Path) -> None:
    # Two true edges: 10->11 and 11->12, with enough wakeups to be confident.
    events = []
    for i in range(8):
        events.append(waking(1_000_000 + i * 1000, waker=10, woken=11))
        events.append(waking(2_000_000 + i * 1000, waker=11, woken=12))
    events.append(end_of_trace(len(events)))
    trace_path = tmp_path / 'pipeline.jsonl'
    write_jsonl(trace_path, events)

    prefix = tmp_path / 'gt'
    Path(f'{prefix}.threads.csv').write_text(
        'tid,role,group,index\n'
        '10,producer,0,0\n'
        '11,worker,0,0\n'
        '12,sink,0,0\n'
    )
    Path(f'{prefix}.handoffs.csv').write_text(
        'group,item_id,from_tid,to_tid,stage,put_ns,get_ns\n'
        '0,0,10,11,producer_to_worker,100,200\n'
        '0,0,11,12,worker_to_sink,300,400\n'
    )

    graph_path = tmp_path / 'graph.json'
    dot_path = tmp_path / 'graph.dot'
    assert main([
        'infer', str(trace_path),
        '--method', 'wakeup',
        '-o', str(graph_path),
        '--dot', str(dot_path),
        '--minimum-count', '5',
    ]) == 0
    assert graph_path.exists()
    assert 'digraph' in dot_path.read_text()

    assert main(['score', str(graph_path), '--groundtruth', str(prefix)]) == 0
