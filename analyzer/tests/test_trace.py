"""Trace loading: sort, target process, outside-process filtering."""

from __future__ import annotations

from pathlib import Path

from analyzer.trace import prepare_trace

from conftest import end_of_trace, switch, waking, write_jsonl


def test_events_are_sorted(tmp_path: Path) -> None:
    events = [
        waking(3000, waker=1, woken=2),
        waking(1000, waker=1, woken=2),
        waking(2000, waker=1, woken=2),
        end_of_trace(3),
    ]
    path = tmp_path / "unsorted.jsonl"
    write_jsonl(path, events)
    trace = prepare_trace(str(path))
    timestamps = [event["timestamp_nanoseconds"] for event in trace.events]
    assert timestamps == sorted(timestamps)
    assert trace.has_end_of_trace is True
    assert trace.recorded_event_count == 3


def test_target_process_from_majority(tmp_path: Path) -> None:
    events = [
        waking(1000, waker=1, woken=2, process_id=100),
        waking(2000, waker=1, woken=2, process_id=100),
        waking(
            3000,
            waker=50,
            woken=51,
            process_id=999,
            woken_process_id=999,
        ),
        end_of_trace(3),
    ]
    path = tmp_path / "majority.jsonl"
    write_jsonl(path, events)
    trace = prepare_trace(str(path))
    assert trace.target_pid == 100
    assert 1 in trace.target_thread_ids
    assert 2 in trace.target_thread_ids
    assert 50 not in trace.target_thread_ids
