"""Load, sort, and filter a collector trace for inference."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from analyzer.trace_reader import is_binary_trace, read_events, read_file_header


@dataclass
class Trace:
    """A sorted, process-filtered view of a collector trace."""

    path: str
    events: list[dict]
    target_process_identifier: int
    # Counts recorded by the collector (from END_OF_TRACE), excluding that record.
    recorded_event_count: int | None = None
    dropped_event_count: int = 0
    has_end_of_trace: bool = False
    # Events whose actor or other involved thread is outside the target process.
    outside_process_event_count: int = 0
    # Thread id -> process id, built from actor and waking payload fields.
    thread_to_process: dict[int, int] = field(default_factory=dict)
    # Thread ids that belong to the target process.
    target_thread_identifiers: set[int] = field(default_factory=set)


def _learn_process_map(events: list[dict]) -> dict[int, int]:
    """Map each seen thread id to a process id from actor and waking fields."""
    thread_to_process: dict[int, int] = {}
    for event in events:
        if event['event_type'] == 'EVENT_TYPE_END_OF_TRACE':
            continue
        thread_identifier = event.get('thread_identifier')
        process_identifier = event.get('process_identifier')
        if thread_identifier and process_identifier:
            thread_to_process[thread_identifier] = process_identifier
        if event['event_type'] == 'EVENT_TYPE_SCHEDULER_WAKING':
            waking = event['scheduler_waking']
            woken_thread = waking['woken_thread_identifier']
            woken_process = waking['woken_process_identifier']
            if woken_thread and woken_process:
                thread_to_process[woken_thread] = woken_process
        if event['event_type'] == 'EVENT_TYPE_SCHEDULER_SWITCH':
            # next_thread_identifier's process is unknown from the switch alone;
            # it will be filled in when that thread is the actor of another event.
            pass
    return thread_to_process


def _pick_target_process(
    path: str,
    events: list[dict],
    thread_to_process: dict[int, int],
) -> int:
    """Use the binary header's target, or the most common process in the events."""
    if is_binary_trace(path):
        header = read_file_header(path)
        target = header['target_process_identifier']
        if target:
            return target
    process_counts: Counter[int] = Counter()
    for event in events:
        if event['event_type'] == 'EVENT_TYPE_END_OF_TRACE':
            continue
        process_identifier = event.get('process_identifier')
        if process_identifier:
            process_counts[process_identifier] += 1
    if not process_counts:
        # Fall back to whatever the process map learned.
        process_counts = Counter(thread_to_process.values())
    if not process_counts:
        raise ValueError(f'{path}: no process identifiers found in the trace')
    return process_counts.most_common(1)[0][0]


def _thread_is_in_target(
    thread_identifier: int,
    target_process_identifier: int,
    thread_to_process: dict[int, int],
    process_identifier: int | None = None,
) -> bool:
    """True if this thread is known to belong to the target process."""
    if thread_identifier == 0:
        return False
    if process_identifier is not None and process_identifier != 0:
        return process_identifier == target_process_identifier
    known = thread_to_process.get(thread_identifier)
    return known == target_process_identifier


def _event_involves_no_target_thread(
    event: dict,
    target_process_identifier: int,
    thread_to_process: dict[int, int],
) -> bool:
    """True if the event names no thread that belongs to the target process.

    Switches from idle onto a target thread, and wakeups of a target thread by
    an outside waker, are kept: inference still needs the target side. Edges
    are formed later only when both endpoints are in-process.
    """
    if event['event_type'] == 'EVENT_TYPE_END_OF_TRACE':
        return False

    if _thread_is_in_target(
        event['thread_identifier'],
        target_process_identifier,
        thread_to_process,
        event.get('process_identifier'),
    ):
        return False
    if event['event_type'] == 'EVENT_TYPE_SCHEDULER_WAKING':
        waking = event['scheduler_waking']
        if _thread_is_in_target(
            waking['woken_thread_identifier'],
            target_process_identifier,
            thread_to_process,
            waking.get('woken_process_identifier'),
        ):
            return False
    if event['event_type'] == 'EVENT_TYPE_SCHEDULER_SWITCH':
        switch = event['scheduler_switch']
        if _thread_is_in_target(
            switch['previous_thread_identifier'],
            target_process_identifier,
            thread_to_process,
        ):
            return False
        if _thread_is_in_target(
            switch['next_thread_identifier'],
            target_process_identifier,
            thread_to_process,
        ):
            return False
    return True


def load_trace(path: str) -> Trace:
    """Read a binary or .jsonl trace, sort it, and keep only in-process events."""
    raw_events = list(read_events(path))
    recorded_event_count: int | None = None
    dropped_event_count = 0
    has_end_of_trace = False
    for event in raw_events:
        if event['event_type'] == 'EVENT_TYPE_END_OF_TRACE':
            has_end_of_trace = True
            end = event['end_of_trace']
            recorded_event_count = end['recorded_event_count']
            dropped_event_count = end['dropped_event_count']

    thread_to_process = _learn_process_map(raw_events)
    target_process_identifier = _pick_target_process(path, raw_events, thread_to_process)
    target_thread_identifiers = {
        thread_identifier
        for thread_identifier, process_identifier in thread_to_process.items()
        if process_identifier == target_process_identifier
    }

    outside_process_event_count = 0
    kept: list[dict] = []
    for event in raw_events:
        if event['event_type'] == 'EVENT_TYPE_END_OF_TRACE':
            continue
        if _event_involves_no_target_thread(event, target_process_identifier, thread_to_process):
            outside_process_event_count += 1
            continue
        kept.append(event)

    kept.sort(key=lambda event: event['timestamp_nanoseconds'])
    return Trace(
        path=path,
        events=kept,
        target_process_identifier=target_process_identifier,
        recorded_event_count=recorded_event_count,
        dropped_event_count=dropped_event_count,
        has_end_of_trace=has_end_of_trace,
        outside_process_event_count=outside_process_event_count,
        thread_to_process=thread_to_process,
        target_thread_identifiers=target_thread_identifiers,
    )
