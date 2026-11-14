"""Load, sort, and filter a collector trace for inference.

The raw file can mix target-app events with wakeups/switches that involve the
idle thread or other processes (the collector keeps events where *either* side
belongs to the target). This module decides which process is "the target",
learns which thread IDs belong to it, drops events that name no target thread
at all, and sorts the rest by timestamp.

Important distinction for newcomers:

- Events that involve one outside thread (e.g. idle → worker switch-in) are
  **kept**. Delay measurement needs those switch-ins.
- Graph **edges** are formed later only when *both* endpoints are in-process
  (see ``wakeup_inference`` / ``timing_inference``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from analyzer.event_decoder import is_binary_trace, read_events, read_file_header


@dataclass
class Trace:
    """A sorted, process-filtered view of a collector trace ready for inference."""

    file_path: str
    events: list[dict]
    target_pid: int
    # From END_OF_TRACE: how many events the collector recorded (excludes that record).
    recorded_event_count: int | None = None
    # From END_OF_TRACE: ring-buffer reservation failures (lost events).
    dropped_event_count: int = 0
    has_end_of_trace: bool = False
    # Events discarded here because no named thread belonged to the target.
    outside_process_event_count: int = 0
    # Thread id -> process id, learned from actor fields and waking payloads.
    thread_to_process: dict[int, int] = field(default_factory=dict)
    # Subset of thread_to_process keys whose process is the target.
    target_thread_ids: set[int] = field(default_factory=set)


def _learn_process_map(events: list[dict]) -> dict[int, int]:
    """Map each seen thread id to a process id from actor and waking fields.

    A ``sched_switch`` alone does not name the next thread's process, so
    ``next_thread_id`` is filled in only when that thread later appears
    as an actor (or as a woken thread on a waking event).
    """
    thread_to_process: dict[int, int] = {}

    for event in events:
        if event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
            continue

        thread_id = event.get("thread_id")
        process_id = event.get("process_id")

        if thread_id and process_id:
            thread_to_process[thread_id] = process_id

        if event["event_type"] == "EVENT_TYPE_SCHEDULER_WAKING":
            # sched_waking fires in the *waker's* context; the woken thread's
            # process is in the payload (see docs/event-format.md).
            waking = event["scheduler_waking"]
            woken_thread = waking["woken_thread_id"]
            woken_process = waking["woken_process_id"]

            if woken_thread and woken_process:
                thread_to_process[woken_thread] = woken_process

    return thread_to_process


def _pick_target_process(
    path: str,
    events: list[dict],
    thread_to_process: dict[int, int],
) -> int:
    """Prefer the binary header's target; else the most common process in the events.

    ``.jsonl`` fixtures have no header, so majority vote is the fallback.
    """
    if is_binary_trace(path):
        header = read_file_header(path)
        target = header["target_pid"]
        if target:
            return target

    process_counts: Counter[int] = Counter()

    for event in events:
        if event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
            continue

        pid = event.get("process_id")

        if pid:
            process_counts[pid] += 1

    # If no process ids found in the trace,
    # use the process ids from the thread_to_process map
    if not process_counts:
        process_counts = Counter(thread_to_process.values())

    # If no process ids found in the trace, raise an error
    if not process_counts:
        raise ValueError(f"{path}: no process ids found in the trace")

    # Return the most common pid
    return process_counts.most_common(1)[0][0]


def _thread_is_in_target(
    thread_id: int,
    target_pid: int,
    thread_to_process: dict[int, int],
    process_id: int | None = None,
) -> bool:
    """True if this thread is known to belong to the target process."""
    # Thread 0 / process 0 is the idle / kernel context, never the application.
    if thread_id == 0:
        return False
    if process_id is not None and process_id != 0:
        return process_id == target_pid
    known = thread_to_process.get(thread_id)
    return known == target_pid


def _event_involves_no_target_thread(
    event: dict,
    target_pid: int,
    thread_to_process: dict[int, int],
) -> bool:
    """True if the event names no thread that belongs to the target process.

    Switches from idle onto a target thread, and wakeups of a target thread by
    an outside waker, are kept: inference still needs the target side. Edges
    are formed later only when both endpoints are in-process.
    """
    if event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
        return False

    if _thread_is_in_target(
        event["thread_id"],
        target_pid,
        thread_to_process,
        event.get("process_id"),
    ):
        return False
    if event["event_type"] == "EVENT_TYPE_SCHEDULER_WAKING":
        waking = event["scheduler_waking"]
        if _thread_is_in_target(
            waking["woken_thread_id"],
            target_pid,
            thread_to_process,
            waking.get("woken_process_id"),
        ):
            return False
    if event["event_type"] == "EVENT_TYPE_SCHEDULER_SWITCH":
        switch = event["scheduler_switch"]
        if _thread_is_in_target(
            switch["previous_thread_id"],
            target_pid,
            thread_to_process,
        ):
            return False
        if _thread_is_in_target(
            switch["next_thread_id"],
            target_pid,
            thread_to_process,
        ):
            return False
    return True


def prepare_trace(path: str) -> Trace:
    """Decode a binary or ``.jsonl`` trace, sort it, and drop pure outside-process noise.

    Steps: decode → read END_OF_TRACE loss counters → learn thread→process map →
    pick target → keep events that involve the target → sort by timestamp.
    """
    raw_events = list(read_events(path))
    recorded_event_count: int | None = None
    dropped_event_count = 0
    has_end_of_trace = False

    for event in raw_events:
        # If the event is an END_OF_TRACE event,
        if event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
            has_end_of_trace = True

            # Set the recorded_event_count and dropped_event_count
            # to the values from the end_of_trace event
            end = event["end_of_trace"]
            recorded_event_count = end["recorded_event_count"]
            dropped_event_count = end["dropped_event_count"]

    thread_to_process = _learn_process_map(raw_events)

    target_pid = _pick_target_process(path, raw_events, thread_to_process)

    target_thread_ids = {
        thread_id
        for thread_id, process_id in thread_to_process.items()
        if process_id == target_pid
    }

    outside_process_event_count = 0
    kept: list[dict] = []
    for event in raw_events:
        if event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
            # Metadata only; not an observation for inference.
            continue

        if _event_involves_no_target_thread(event, target_pid, thread_to_process):
            outside_process_event_count += 1
            continue

        kept.append(event)

    # Ring buffer can deliver records slightly out of timestamp order across CPUs.
    kept.sort(key=lambda event: event["timestamp_nanoseconds"])
    return Trace(
        file_path=path,
        events=kept,
        target_pid=target_pid,
        recorded_event_count=recorded_event_count,
        dropped_event_count=dropped_event_count,
        has_end_of_trace=has_end_of_trace,
        outside_process_event_count=outside_process_event_count,
        thread_to_process=thread_to_process,
        target_thread_ids=target_thread_ids,
    )
