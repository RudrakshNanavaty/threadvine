"""Infer thread relationships from ``sched_waking`` events.

A wakeup A → B means the kernel ran ``sched_waking`` in A's context for B.
In the validation benchmark, producers wake workers (and workers wake sinks)
via condition-variable signals. The same queues also cause *reverse* wakeups
when full (taker signals the putter) — so raw wakeup direction is not always
the work-flow direction.

This method:

1. Counts in-process wakeups per ordered pair (both ends in the target process).
2. Measures wakeup-to-execution delay (waking timestamp → next switch-in of B).
3. Collapses each unordered pair into one directed edge in the **dominant**
   direction, keeping the minority count as ``reverse_wakeup_count``.
4. Applies confidence thresholds via ``Graph.apply_thresholds``.

Outside wakers (idle, softirq) never form edges: they are not in
``trace.target_thread_identifiers``.
"""

from __future__ import annotations

from collections import defaultdict

from analyzer.graph import (
    Edge,
    Graph,
    GraphMetadata,
    InferenceSettings,
    ThreadSummary,
    summarize_delays_nanoseconds,
)
from analyzer.trace import Trace


def infer(trace: Trace, settings: InferenceSettings) -> Graph:
    """Count in-process wakeups, pick the dominant direction, measure delays."""
    # Ordered pair (waker, woken) -> count.
    wakeup_counts: dict[tuple[int, int], int] = defaultdict(int)
    # Delays from waking to next switch-in, keyed by ordered pair and by woken thread.
    delays_by_pair: dict[tuple[int, int], list[int]] = defaultdict(list)
    delays_by_thread: dict[int, list[int]] = defaultdict(list)

    # Precompute switch-in times per thread so each waking can find the next one.
    # next_thread_identifier is who is about to run on that CPU.
    switch_in_times: dict[int, list[int]] = defaultdict(list)
    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_SWITCH":
            continue
        next_thread = event["scheduler_switch"]["next_thread_identifier"]
        if next_thread in trace.target_thread_identifiers:
            switch_in_times[next_thread].append(event["timestamp_nanoseconds"])

    # Monotone cursors: events are time-sorted, so we never scan backward.
    switch_in_index: dict[int, int] = defaultdict(int)

    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_WAKING":
            continue
        waker = event["thread_identifier"]
        woken = event["scheduler_waking"]["woken_thread_identifier"]
        if waker == woken:
            continue
        # Edges require both endpoints inside the traced application.
        if waker not in trace.target_thread_identifiers:
            continue
        if woken not in trace.target_thread_identifiers:
            continue
        wakeup_counts[(waker, woken)] += 1

        waking_time = event["timestamp_nanoseconds"]
        times = switch_in_times.get(woken, [])
        index = switch_in_index[woken]
        while index < len(times) and times[index] < waking_time:
            index += 1
        switch_in_index[woken] = index
        if index < len(times):
            delay_nanoseconds = times[index] - waking_time
            delays_by_pair[(waker, woken)].append(delay_nanoseconds)
            delays_by_thread[woken].append(delay_nanoseconds)

    # One edge per unordered pair; ties break toward the lower thread id.
    seen_pairs: set[frozenset[int]] = set()
    edges: list[Edge] = []
    for (waker, woken), forward_count in list(wakeup_counts.items()):
        pair_key = frozenset((waker, woken))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        reverse_count = wakeup_counts.get((woken, waker), 0)
        if forward_count > reverse_count or (
            forward_count == reverse_count and waker < woken
        ):
            source, destination = waker, woken
            primary, reverse = forward_count, reverse_count
            delay_samples = delays_by_pair.get((waker, woken), [])
        else:
            source, destination = woken, waker
            primary, reverse = reverse_count, forward_count
            delay_samples = delays_by_pair.get((woken, waker), [])
        edges.append(
            Edge(
                source_thread_identifier=source,
                destination_thread_identifier=destination,
                wakeup_count=primary,
                reverse_wakeup_count=reverse,
                delay=summarize_delays_nanoseconds(delay_samples),
            )
        )

    threads: dict[int, ThreadSummary] = {}
    for thread_identifier in trace.target_thread_identifiers:
        threads[thread_identifier] = ThreadSummary(
            thread_identifier=thread_identifier,
            delay=summarize_delays_nanoseconds(
                delays_by_thread.get(thread_identifier, [])
            ),
        )

    graph = Graph(
        metadata=GraphMetadata(
            method="wakeup",
            minimum_count=settings.minimum_count,
            minimum_fraction=settings.minimum_fraction,
            timing_window_us=None,
            trace_path=trace.path,
            target_process_identifier=trace.target_process_identifier,
            recorded_event_count=trace.recorded_event_count,
            dropped_event_count=trace.dropped_event_count,
            has_end_of_trace=trace.has_end_of_trace,
            outside_process_event_count=trace.outside_process_event_count,
        ),
        threads=threads,
        edges=edges,
    )
    graph.apply_thresholds(settings)
    return graph
