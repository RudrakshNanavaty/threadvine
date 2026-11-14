"""Wakeup inference: direction, filtering, thresholds, out-of-order events."""
from __future__ import annotations

from pathlib import Path

from analyzer.graph import InferenceSettings
from analyzer.trace import prepare_trace
from analyzer.wakeup_inference import infer

from conftest import end_of_trace, switch, waking, write_jsonl


def _settings(**overrides) -> InferenceSettings:
    values = dict(method='wakeup', minimum_count=5, minimum_fraction=0.05)
    values.update(overrides)
    return InferenceSettings(**values)


def test_dominant_direction_with_reverse_wakeups(tmp_path: Path) -> None:
    # Producer (1) wakes worker (2) often; worker sometimes wakes producer (backpressure).
    events = []
    for i in range(10):
        events.append(waking(1_000_000 + i * 1000, waker=1, woken=2))
        events.append(switch(1_000_500 + i * 1000, previous_thread=2, next_thread=2, previous_state=1))
    for i in range(3):
        events.append(waking(2_000_000 + i * 1000, waker=2, woken=1))
        events.append(switch(2_000_500 + i * 1000, previous_thread=1, next_thread=1, previous_state=1))
    events.append(end_of_trace(len(events)))
    # Deliberately out of order in the file.
    events = list(reversed(events[:-1])) + [events[-1]]
    path = tmp_path / 'dominant.jsonl'
    write_jsonl(path, events)

    graph = infer(prepare_trace(str(path)), _settings(minimum_count=5))
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.source_thread_id == 1
    assert edge.destination_thread_id == 2
    assert edge.wakeup_count == 10
    assert edge.reverse_wakeup_count == 3
    assert edge.confidence == 'confident'


def test_outside_process_wakers_do_not_form_edges(tmp_path: Path) -> None:
    events = [
        # Idle / other-process wake a target thread: kept for delay context,
        # but must not become graph edges.
        waking(1_000, waker=0, woken=2, process_id=0, woken_process_id=100),
        waking(2_000, waker=99, woken=2, process_id=1, woken_process_id=100),
        # Pure outside noise: neither side is the target.
        waking(3_000, waker=50, woken=51, process_id=999, woken_process_id=999),
        # Real in-process wakeups, enough to be confident.
        *[waking(10_000 + i * 100, waker=1, woken=2) for i in range(6)],
        *[
            switch(10_050 + i * 100, previous_thread=3, next_thread=2, previous_state=0)
            for i in range(6)
        ],
        end_of_trace(15),
    ]
    path = tmp_path / 'outside.jsonl'
    write_jsonl(path, events)

    trace = prepare_trace(str(path))
    assert trace.outside_process_event_count >= 1
    graph = infer(trace, _settings(minimum_count=5))
    pairs = {
        (edge.source_thread_id, edge.destination_thread_id)
        for edge in graph.edges
    }
    assert (0, 2) not in pairs
    assert (99, 2) not in pairs
    assert (50, 51) not in pairs
    assert (1, 2) in pairs


def test_missing_end_of_trace_is_recorded(tmp_path: Path) -> None:
    events = [waking(1000 + i * 100, waker=1, woken=2) for i in range(6)]
    path = tmp_path / 'cut_short.jsonl'
    write_jsonl(path, events)

    graph = infer(prepare_trace(str(path)), _settings())
    assert graph.metadata.has_end_of_trace is False
    assert graph.loss_warnings()


def test_ambiguous_below_count_threshold(tmp_path: Path) -> None:
    events = [waking(1000 + i * 100, waker=1, woken=2) for i in range(4)]
    events.append(end_of_trace(4))
    path = tmp_path / 'below.jsonl'
    write_jsonl(path, events)

    graph = infer(prepare_trace(str(path)), _settings(minimum_count=5))
    assert len(graph.edges) == 1
    assert graph.edges[0].confidence == 'ambiguous'


def test_ambiguous_below_fraction_threshold(tmp_path: Path) -> None:
    # Thread 2 receives 100 wakeups from thread 1 and 4 from thread 3.
    # 4 / 104 < 0.05, so 3->2 is ambiguous even though count is below minimum
    # when we set minimum_count=1.
    events = [waking(1000 + i, waker=1, woken=2) for i in range(100)]
    events += [waking(2000 + i, waker=3, woken=2) for i in range(4)]
    events.append(end_of_trace(104))
    path = tmp_path / 'fraction.jsonl'
    write_jsonl(path, events)

    graph = infer(
        prepare_trace(str(path)),
        _settings(minimum_count=1, minimum_fraction=0.05),
    )
    by_pair = {
        (edge.source_thread_id, edge.destination_thread_id): edge
        for edge in graph.edges
    }
    assert by_pair[(1, 2)].confidence == 'confident'
    assert by_pair[(3, 2)].confidence == 'ambiguous'


def test_wakeup_to_execution_delay(tmp_path: Path) -> None:
    events = [
        waking(1_000_000, waker=1, woken=2),
        switch(1_050_000, previous_thread=9, next_thread=2),  # 50 us later
        waking(2_000_000, waker=1, woken=2),
        switch(2_100_000, previous_thread=9, next_thread=2),  # 100 us later
        waking(3_000_000, waker=1, woken=2),
        switch(3_200_000, previous_thread=9, next_thread=2),  # 200 us later
        waking(4_000_000, waker=1, woken=2),
        switch(4_050_000, previous_thread=9, next_thread=2),
        waking(5_000_000, waker=1, woken=2),
        switch(5_050_000, previous_thread=9, next_thread=2),
        end_of_trace(10),
    ]
    # Also register thread 9 as in-process so switch events for next_thread=2
    # whose actor is 9 are kept. Give 9 the same process id.
    for event in events:
        if event['event_type'] == 'EVENT_TYPE_SCHEDULER_SWITCH':
            event['thread_id'] = 9
            event['process_id'] = 100
            event['scheduler_switch']['previous_thread_id'] = 9
    # Make sure thread 2 is known as process 100 via a waking or actor role.
    path = tmp_path / 'delay.jsonl'
    write_jsonl(path, events)

    graph = infer(prepare_trace(str(path)), _settings(minimum_count=5))
    edge = graph.edges[0]
    assert edge.delay.sample_count == 5
    assert edge.delay.max_us == 200.0
    assert edge.delay.p50_us is not None
