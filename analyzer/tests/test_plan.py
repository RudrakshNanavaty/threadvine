"""Smoke test: ``plan`` gives each group a disjoint CPU block."""
from __future__ import annotations

from analyzer.graph import Edge, Graph, GraphMetadata
from analyzer.plan import build_plan


def test_plan_gives_each_group_a_disjoint_cpu_block() -> None:
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
    graph = Graph(
        metadata=metadata,
        edges=[
            Edge(10, 11, confidence='confident', wakeup_count=9),
            Edge(11, 12, confidence='confident', wakeup_count=9),
            Edge(20, 21, confidence='confident', wakeup_count=9),
            Edge(21, 22, confidence='confident', wakeup_count=9),
            # Ambiguous join wakeup into main must not merge the two groups.
            Edge(12, 1, confidence='ambiguous', wakeup_count=1),
            Edge(22, 1, confidence='ambiguous', wakeup_count=1),
        ],
    )

    plan = build_plan(graph, cpu_count=4)

    assignments = plan['assignments']
    group_zero = {tuple(assignments[str(thread)]) for thread in (10, 11, 12)}
    group_one = {tuple(assignments[str(thread)]) for thread in (20, 21, 22)}
    assert group_zero == {(0, 1)}
    assert group_one == {(2, 3)}
    assert plan['unpinned'] == [1]
    assert plan['cpu_count'] == 4
