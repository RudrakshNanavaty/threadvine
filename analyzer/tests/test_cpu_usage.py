"""CPU share per thread from scheduler switches."""
from __future__ import annotations

from pathlib import Path

from analyzer.cpu_usage import thread_cpu_shares
from analyzer.trace import prepare_trace

from conftest import end_of_trace, switch, write_jsonl


def test_busy_thread_has_a_high_share_and_a_mostly_sleeping_thread_a_low_one(tmp_path: Path) -> None:
    millisecond = 1_000_000
    events = [
        # Thread 10 runs on CPU 0 the whole time (switched in at 0, out at 100 ms).
        switch(0, previous_thread=0, next_thread=10, cpu_index=0),
        # Thread 11 runs on CPU 1 for 10 of the 100 ms.
        switch(10 * millisecond, previous_thread=0, next_thread=11, cpu_index=1),
        switch(20 * millisecond, previous_thread=11, next_thread=0, cpu_index=1),
        switch(100 * millisecond, previous_thread=10, next_thread=0, cpu_index=0),
    ]
    events.append(end_of_trace(len(events)))
    trace_path = tmp_path / 'shares.jsonl'
    write_jsonl(trace_path, events)

    shares = thread_cpu_shares(prepare_trace(str(trace_path)))

    assert shares[10] == 1.0
    assert abs(shares[11] - 0.10) < 1e-9


def test_a_switch_out_without_a_recorded_switch_in_is_not_counted_from_trace_start(tmp_path: Path) -> None:
    millisecond = 1_000_000
    events = [
        switch(0, previous_thread=0, next_thread=10, cpu_index=0),
        switch(10 * millisecond, previous_thread=10, next_thread=900, cpu_index=0),
        # Thread 900 is outside the process, so its later switch-in to thread 11 is recorded
        # but the switch that put 900 on this CPU (from another outside thread) is not.
        switch(50 * millisecond, previous_thread=901, next_thread=11, cpu_index=0),
        # Thread 11 goes out; its switch-in above was recorded, so it ran 50 -> 60 ms.
        switch(60 * millisecond, previous_thread=11, next_thread=900, cpu_index=0),
        switch(100 * millisecond, previous_thread=900, next_thread=0, cpu_index=0),
    ]
    events.append(end_of_trace(len(events)))
    trace_path = tmp_path / 'gap.jsonl'
    write_jsonl(trace_path, events)

    shares = thread_cpu_shares(prepare_trace(str(trace_path)))

    assert abs(shares[10] - 0.10) < 1e-9
    assert abs(shares[11] - 0.10) < 1e-9
