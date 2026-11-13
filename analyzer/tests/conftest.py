"""Shared helpers for analyzer tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def fixtures_directory() -> Path:
    return FIXTURES


def write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open('w') as output_file:
        for event in events:
            output_file.write(json.dumps(event) + '\n')


def waking(
    timestamp_nanoseconds: int,
    waker: int,
    woken: int,
    process_identifier: int = 100,
    woken_process_identifier: int | None = None,
    cpu_index: int = 0,
) -> dict:
    if woken_process_identifier is None:
        woken_process_identifier = process_identifier
    return {
        'timestamp_nanoseconds': timestamp_nanoseconds,
        'event_type': 'EVENT_TYPE_SCHEDULER_WAKING',
        'cpu_index': cpu_index,
        'thread_identifier': waker,
        'process_identifier': process_identifier,
        'scheduler_waking': {
            'woken_thread_identifier': woken,
            'woken_process_identifier': woken_process_identifier,
        },
    }


def switch(
    timestamp_nanoseconds: int,
    previous_thread: int,
    next_thread: int,
    previous_state: int = 0,
    process_identifier: int = 100,
    cpu_index: int = 0,
) -> dict:
    return {
        'timestamp_nanoseconds': timestamp_nanoseconds,
        'event_type': 'EVENT_TYPE_SCHEDULER_SWITCH',
        'cpu_index': cpu_index,
        'thread_identifier': previous_thread,
        'process_identifier': process_identifier,
        'scheduler_switch': {
            'previous_thread_identifier': previous_thread,
            'next_thread_identifier': next_thread,
            'previous_thread_state': previous_state,
        },
    }


def end_of_trace(
    recorded_event_count: int,
    dropped_event_count: int = 0,
    timestamp_nanoseconds: int = 0,
) -> dict:
    return {
        'timestamp_nanoseconds': timestamp_nanoseconds,
        'event_type': 'EVENT_TYPE_END_OF_TRACE',
        'cpu_index': 0,
        'thread_identifier': 0,
        'process_identifier': 0,
        'end_of_trace': {
            'recorded_event_count': recorded_event_count,
            'dropped_event_count': dropped_event_count,
        },
    }
