"""Infer thread relationships from block-then-start timing alone.

This is the proposal's "timing-only" baseline: it uses **no** wakeup information.
The heuristic is "A stopped (blocked), then B started soon after," which can
suggest a handoff but also fires for unrelated co-scheduling.

Algorithm:

1. When A switches out with ``previous_thread_state != 0`` (blocked — not
   preempted/runnable), look at every other target thread B that switches in
   on any CPU within ``--timing-window-us`` (default 100 µs).
2. Count each such A → B observation.
3. Collapse unordered pairs to the dominant direction (same as wakeup).
4. Apply confidence thresholds.

Kernel task-state bits (raw ``previous_thread_state``): ``0`` means runnable /
preempted; non-zero means blocked (sleeping, uninterruptible, etc.).
"""

from __future__ import annotations

from collections import defaultdict

from analyzer.graph import (
    Edge,
    Graph,
    GraphMetadata,
    InferenceSettings,
    ThreadSummary,
)
from analyzer.trace import Trace


def infer(trace: Trace, settings: InferenceSettings) -> Graph:
    """Count A→B when A blocks and B switches in within the timing window."""
    window_nanoseconds = settings.timing_window_us * 1000
    timing_counts: dict[tuple[int, int], int] = defaultdict(int)

    # All target-thread switch-ins in time order (trace.events is already sorted).
    switch_ins: list[tuple[int, int]] = []  # (timestamp_nanoseconds, thread_id)
    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_SWITCH":
            continue
        next_thread = event["scheduler_switch"]["next_thread_id"]
        if next_thread in trace.target_thread_ids:
            switch_ins.append((event["timestamp_nanoseconds"], next_thread))

    # Sliding cursor: only switch-ins at or after the current block time matter.
    switch_in_index = 0
    for event in trace.events:
        if event["event_type"] != "EVENT_TYPE_SCHEDULER_SWITCH":
            continue
        switch = event["scheduler_switch"]
        previous_thread = switch["previous_thread_id"]
        previous_state = switch["previous_thread_state"]
        if previous_thread not in trace.target_thread_ids:
            continue
        if previous_state == 0:
            # Runnable / preempted: A did not block waiting for someone else.
            continue
        block_time = event["timestamp_nanoseconds"]
        while (
            switch_in_index < len(switch_ins)
            and switch_ins[switch_in_index][0] < block_time
        ):
            switch_in_index += 1
        cursor = switch_in_index
        while cursor < len(switch_ins):
            switch_in_time, next_thread = switch_ins[cursor]
            if switch_in_time - block_time > window_nanoseconds:
                break
            if next_thread != previous_thread:
                timing_counts[(previous_thread, next_thread)] += 1
            cursor += 1

    # Same dominant-direction collapse as wakeup_inference.
    seen_pairs: set[frozenset[int]] = set()
    edges: list[Edge] = []
    for (source, destination), forward_count in list(timing_counts.items()):
        pair_key = frozenset((source, destination))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        reverse_count = timing_counts.get((destination, source), 0)
        if forward_count > reverse_count or (
            forward_count == reverse_count and source < destination
        ):
            edges.append(
                Edge(
                    source_thread_id=source,
                    destination_thread_id=destination,
                    timing_count=forward_count,
                )
            )
        else:
            edges.append(
                Edge(
                    source_thread_id=destination,
                    destination_thread_id=source,
                    timing_count=reverse_count,
                )
            )

    threads = {
        thread_id: ThreadSummary(thread_id=thread_id)
        for thread_id in trace.target_thread_ids
    }
    graph = Graph(
        metadata=GraphMetadata(
            method="timing",
            minimum_count=settings.minimum_count,
            minimum_fraction=settings.minimum_fraction,
            timing_window_us=settings.timing_window_us,
            trace_path=trace.file_path,
            target_pid=trace.target_pid,
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
