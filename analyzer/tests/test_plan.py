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


def _two_group_graph() -> Graph:
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
            Edge(10, 11, confidence='confident', wakeup_count=9),
            Edge(11, 12, confidence='confident', wakeup_count=9),
            Edge(20, 21, confidence='confident', wakeup_count=9),
            Edge(21, 22, confidence='confident', wakeup_count=9),
        ],
    )


def test_plan_isolates_a_cpu_hog_from_the_cooperating_groups() -> None:
    graph = _two_group_graph()
    # Threads 98 and 99 burn a whole CPU each and talk to nobody; the rest are nearly idle.
    shares = {10: 0.02, 11: 0.03, 12: 0.02, 20: 0.02, 21: 0.03, 22: 0.02, 98: 1.0, 99: 0.98}

    plan = build_plan(graph, cpu_count=4, cpu_share_by_thread=shares)

    assignments = plan['assignments']
    hog_cpus = set(assignments['98']) | set(assignments['99'])
    group_cpus = {cpu for thread in (10, 11, 12, 20, 21, 22) for cpu in assignments[str(thread)]}
    assert hog_cpus == {0, 1}
    assert group_cpus == {2, 3}
    assert hog_cpus.isdisjoint(group_cpus)


def test_plan_without_headroom_falls_back_to_the_plain_plan() -> None:
    graph = _two_group_graph()
    # Every thread is hot and together they need more than 4 CPUs: nothing to isolate.
    shares = {10: 0.9, 11: 0.9, 12: 0.9, 20: 0.9, 21: 0.9, 22: 0.9}

    assert build_plan(graph, 4, shares) == build_plan(graph, 4)


def test_plan_puts_both_groups_on_the_one_cpu_left_after_isolating_hogs() -> None:
    graph = _two_group_graph()
    shares = {10: 0.02, 11: 0.03, 12: 0.02, 20: 0.02, 21: 0.03, 22: 0.02, 97: 1.0, 98: 1.0, 99: 1.0}

    plan = build_plan(graph, cpu_count=4, cpu_share_by_thread=shares)

    assignments = plan['assignments']
    assert {tuple(assignments[str(thread)]) for thread in (97, 98, 99)} == {(0, 1, 2)}
    # Two groups, one CPU left: both share it instead of leaving one group floating.
    assert {tuple(assignments[str(thread)]) for thread in (10, 11, 12, 20, 21, 22)} == {(3,)}
    assert plan['unpinned'] == []
