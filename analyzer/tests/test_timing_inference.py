"""Timing-only inference: block-then-start within a window."""
from __future__ import annotations

from pathlib import Path

from analyzer.graph import InferenceSettings
from analyzer.timing_inference import infer
from analyzer.trace import load_trace

from conftest import end_of_trace, switch, waking, write_jsonl


def test_block_then_start_within_window(tmp_path: Path) -> None:
    # Establish process membership with a couple of wakeups (not used by timing).
    events = [
        waking(100, waker=1, woken=2),
        waking(200, waker=2, woken=3),
        # Thread 1 blocks (state S=1), then thread 2 switches in 50 us later.
        switch(1_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(1_050_000, previous_thread=9, next_thread=2, previous_state=0),
        switch(2_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(2_050_000, previous_thread=9, next_thread=2, previous_state=0),
        switch(3_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(3_050_000, previous_thread=9, next_thread=2, previous_state=0),
        switch(4_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(4_050_000, previous_thread=9, next_thread=2, previous_state=0),
        switch(5_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(5_050_000, previous_thread=9, next_thread=2, previous_state=0),
        # Outside the window: 200 us later should not count with window=100 us.
        switch(6_000_000, previous_thread=1, next_thread=9, previous_state=1),
        switch(6_200_000, previous_thread=9, next_thread=2, previous_state=0),
        # Preempt (state 0) should not count.
        switch(7_000_000, previous_thread=1, next_thread=9, previous_state=0),
        switch(7_050_000, previous_thread=9, next_thread=2, previous_state=0),
        end_of_trace(16),
    ]
    # Thread 9 must be in-process so switch events are kept.
    for event in events:
        if event['event_type'] == 'EVENT_TYPE_SCHEDULER_SWITCH':
            event['process_identifier'] = 100
    path = tmp_path / 'timing.jsonl'
    write_jsonl(path, events)

    settings = InferenceSettings(
        method='timing',
        minimum_count=5,
        minimum_fraction=0.05,
        timing_window_us=100,
    )
    graph = infer(load_trace(str(path)), settings)
    by_pair = {
        (edge.source_thread_identifier, edge.destination_thread_identifier): edge
        for edge in graph.edges
    }
    assert (1, 2) in by_pair
    assert by_pair[(1, 2)].timing_count == 5
    assert by_pair[(1, 2)].confidence == 'confident'
