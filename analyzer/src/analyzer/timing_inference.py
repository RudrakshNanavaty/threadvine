"""Infer thread relationships from block-then-start timing alone."""
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
    """Count A->B when A blocks and B switches in within the timing window."""
    window_nanoseconds = settings.timing_window_us * 1000
    timing_counts: dict[tuple[int, int], int] = defaultdict(int)

    # Collect switch-in events for target threads, sorted (trace.events is sorted).
    switch_ins: list[tuple[int, int]] = []  # (timestamp_nanoseconds, thread_identifier)
    for event in trace.events:
        if event['event_type'] != 'EVENT_TYPE_SCHEDULER_SWITCH':
            continue
        next_thread = event['scheduler_switch']['next_thread_identifier']
        if next_thread in trace.target_thread_identifiers:
            switch_ins.append((event['timestamp_nanoseconds'], next_thread))

    switch_in_index = 0
    for event in trace.events:
        if event['event_type'] != 'EVENT_TYPE_SCHEDULER_SWITCH':
            continue
        switch = event['scheduler_switch']
        previous_thread = switch['previous_thread_identifier']
        previous_state = switch['previous_thread_state']
        if previous_thread not in trace.target_thread_identifiers:
            continue
        if previous_state == 0:
            # Runnable / preempted: not a block.
            continue
        block_time = event['timestamp_nanoseconds']
        # Advance the switch-in cursor past times before this block.
        while switch_in_index < len(switch_ins) and switch_ins[switch_in_index][0] < block_time:
            switch_in_index += 1
        cursor = switch_in_index
        while cursor < len(switch_ins):
            switch_in_time, next_thread = switch_ins[cursor]
            if switch_in_time - block_time > window_nanoseconds:
                break
            if next_thread != previous_thread:
                timing_counts[(previous_thread, next_thread)] += 1
            cursor += 1

    # Collapse each unordered pair into the dominant direction.
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
                    source_thread_identifier=source,
                    destination_thread_identifier=destination,
                    timing_count=forward_count,
                )
            )
        else:
            edges.append(
                Edge(
                    source_thread_identifier=destination,
                    destination_thread_identifier=source,
                    timing_count=reverse_count,
                )
            )

    threads = {
        thread_identifier: ThreadSummary(thread_identifier=thread_identifier)
        for thread_identifier in trace.target_thread_identifiers
    }
    graph = Graph(
        metadata=GraphMetadata(
            method='timing',
            minimum_count=settings.minimum_count,
            minimum_fraction=settings.minimum_fraction,
            timing_window_us=settings.timing_window_us,
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
