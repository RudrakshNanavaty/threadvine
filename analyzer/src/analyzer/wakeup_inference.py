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
   direction, keeping the minority count as ``reverse_wakeup_count``. With
   ``orientation="first-wakeup"`` (opt-in) a pair that has at least
   ``minimum_count`` wakeups both ways is instead oriented by whichever direction
   woke first: full queues make the *busier* direction the backpressure one.
4. Applies confidence thresholds via ``Graph.apply_thresholds``.

Outside wakers (idle, softirq) never form edges: they are not in
``trace.target_thread_ids``.
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
    # Ordered pair -> timestamp (ns) of its first wakeup, for the first-wakeup orientation.
    first_wakeup_time: dict[tuple[int, int], int] = {}
    # Delays from waking to next switch-in, keyed by ordered pair and by woken thread.
    delays_by_pair: dict[tuple[int, int], list[int]] = defaultdict(list)
    delays_by_thread: dict[int, list[int]] = defaultdict(list)

    # Precompute switch-in times per thread so each waking can find the next one.
    # next_thread_id is who is about to run on that CPU.
    switch_in_times: dict[int, list[int]] = defaultdict(list)
    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_SWITCH":
            continue
        next_thread = event["scheduler_switch"]["next_thread_id"]
        if next_thread in trace.target_thread_ids:
            switch_in_times[next_thread].append(event["timestamp_nanoseconds"])

    # Monotone cursors: events are time-sorted, so we never scan backward.
    switch_in_index: dict[int, int] = defaultdict(int)

    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_WAKING":
            continue
        waker = event["thread_id"]
        woken = event["scheduler_waking"]["woken_thread_id"]
        if waker == woken:
            continue
        # Edges require both endpoints inside the traced application.
        if waker not in trace.target_thread_ids:
            continue
        if woken not in trace.target_thread_ids:
            continue
        wakeup_counts[(waker, woken)] += 1

        waking_time = event["timestamp_nanoseconds"]
        first_wakeup_time.setdefault((waker, woken), waking_time)
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
        if (
            settings.orientation == "first-wakeup"
            and forward_count >= settings.minimum_count
            and reverse_count >= settings.minimum_count
        ):
            # Both directions are real: with full queues the busier one is the
            # backpressure wakeup, so trust whichever direction woke first.
            forward_first = first_wakeup_time[(waker, woken)]
            reverse_first = first_wakeup_time[(woken, waker)]
            use_forward = forward_first < reverse_first or (
                forward_first == reverse_first and waker < woken
            )
        else:
            use_forward = forward_count > reverse_count or (
                forward_count == reverse_count and waker < woken
            )
        if use_forward:
            source, destination = waker, woken
            primary, reverse = forward_count, reverse_count
            delay_samples = delays_by_pair.get((waker, woken), [])
        else:
            source, destination = woken, waker
            primary, reverse = reverse_count, forward_count
            delay_samples = delays_by_pair.get((woken, waker), [])
        edges.append(
            Edge(
                source_thread_id=source,
                destination_thread_id=destination,
                wakeup_count=primary,
                reverse_wakeup_count=reverse,
                delay=summarize_delays_nanoseconds(delay_samples),
            )
        )

    threads: dict[int, ThreadSummary] = {}
    for thread_id in trace.target_thread_ids:
        threads[thread_id] = ThreadSummary(
            thread_id=thread_id,
            delay=summarize_delays_nanoseconds(
                delays_by_thread.get(thread_id, [])
            ),
        )

    graph = Graph(
        metadata=GraphMetadata(
            method="wakeup",
            minimum_count=settings.minimum_count,
            minimum_fraction=settings.minimum_fraction,
            timing_window_us=None,
            trace_path=trace.file_path,
            target_pid=trace.target_pid,
            recorded_event_count=trace.recorded_event_count,
            dropped_event_count=trace.dropped_event_count,
            has_end_of_trace=trace.has_end_of_trace,
            outside_process_event_count=trace.outside_process_event_count,
            minimum_source_fraction=settings.minimum_source_fraction,
            orientation=settings.orientation,
        ),
        threads=threads,
        edges=edges,
    )
    graph.apply_thresholds(settings)
    return graph
