"""Futex events: binary decoding, trace loading, and being ignored by wakeup inference."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from analyzer.graph import InferenceSettings
from analyzer.trace import prepare_trace
from analyzer.event_decoder import FILE_HEADER, FUTEX_OPERATION_NAMES, main, read_events
from analyzer.wakeup_inference import infer

from conftest import end_of_trace, switch, waking, write_jsonl

# Mirrors struct event in bpf/event.h: a 24-byte header and a 16-byte payload.
EVENT_HEADER = struct.Struct("<QIIII")
EVENT_TYPE_FUTEX_ENTER = 3
EVENT_TYPE_FUTEX_EXIT = 4
EVENT_TYPE_END_OF_TRACE = 0xFFFF
PROCESS_ID = 100


def _binary_record(
    timestamp_nanoseconds: int,
    event_type: int,
    thread_id: int,
    payload: bytes,
    process_id: int = PROCESS_ID,
    cpu_index: int = 1,
) -> bytes:
    header = EVENT_HEADER.pack(
        timestamp_nanoseconds,
        event_type,
        cpu_index,
        thread_id,
        process_id,
    )
    return header + payload.ljust(16, b"\0")


def _futex_enter(
    timestamp_nanoseconds: int,
    thread_id: int,
    futex_address: int,
    futex_operation: int,
    process_id: int = PROCESS_ID,
) -> bytes:
    # futex_address (u64), futex_operation (u32), reserved (u32)
    payload = struct.pack("<QII", futex_address, futex_operation, 0)
    return _binary_record(
        timestamp_nanoseconds,
        EVENT_TYPE_FUTEX_ENTER,
        thread_id,
        payload,
        process_id,
    )


def _futex_exit(
    timestamp_nanoseconds: int,
    thread_id: int,
    return_value: int,
    process_id: int = PROCESS_ID,
) -> bytes:
    # return_value (s64), reserved (u64)
    payload = struct.pack("<qQ", return_value, 0)
    return _binary_record(
        timestamp_nanoseconds,
        EVENT_TYPE_FUTEX_EXIT,
        thread_id,
        payload,
        process_id,
    )


def _end_record(recorded_event_count: int, dropped_event_count: int = 0) -> bytes:
    payload = struct.pack("<QQ", recorded_event_count, dropped_event_count)
    return _binary_record(0, EVENT_TYPE_END_OF_TRACE, 0, payload, process_id=0)


def _write_binary_trace(path: Path, records: list[bytes]) -> None:
    file_header = FILE_HEADER.pack(
        b"TRPF", 1, EVENT_HEADER.size + 16, 1, PROCESS_ID, 0, 0
    )
    path.write_bytes(file_header + b"".join(records))


def _futex_enter_dictionary(
    timestamp_nanoseconds: int, thread: int, address: int, operation: int
) -> dict:
    return {
        "timestamp_nanoseconds": timestamp_nanoseconds,
        "event_type": "EVENT_TYPE_FUTEX_ENTER",
        "cpu_index": 0,
        "thread_id": thread,
        "process_id": PROCESS_ID,
        "futex_enter": {"futex_address": address, "futex_operation": operation},
    }


def _futex_exit_dictionary(
    timestamp_nanoseconds: int, thread: int, return_value: int
) -> dict:
    return {
        "timestamp_nanoseconds": timestamp_nanoseconds,
        "event_type": "EVENT_TYPE_FUTEX_EXIT",
        "cpu_index": 0,
        "thread_id": thread,
        "process_id": PROCESS_ID,
        "futex_exit": {"return_value": return_value},
    }


def test_binary_futex_enter_and_exit_decode(tmp_path: Path) -> None:
    path = tmp_path / "futex.events.bin"
    _write_binary_trace(
        path,
        [
            _futex_enter(1000, 7, 0x5A68EEF32048, 9),  # FUTEX_WAIT_BITSET
            _futex_exit(2500, 7, 0),
            _futex_enter(3000, 8, 0x5A68EEF32048, 1),  # FUTEX_WAKE
            _futex_exit(3100, 8, 1),  # one thread woken
            _end_record(4),
        ],
    )

    events = list(read_events(str(path)))

    assert [event["event_type"] for event in events] == [
        "EVENT_TYPE_FUTEX_ENTER",
        "EVENT_TYPE_FUTEX_EXIT",
        "EVENT_TYPE_FUTEX_ENTER",
        "EVENT_TYPE_FUTEX_EXIT",
        "EVENT_TYPE_END_OF_TRACE",
    ]
    first_enter = events[0]
    assert first_enter["timestamp_nanoseconds"] == 1000
    assert first_enter["cpu_index"] == 1
    assert first_enter["thread_id"] == 7
    assert first_enter["process_id"] == PROCESS_ID
    # Reserved payload bytes are omitted, as for every other event type.
    assert first_enter["futex_enter"] == {
        "futex_address": 0x5A68EEF32048,
        "futex_operation": 9,
    }
    assert events[1]["futex_exit"] == {"return_value": 0}
    assert events[2]["futex_enter"]["futex_operation"] == 1
    assert events[3]["futex_exit"] == {"return_value": 1}


def test_futex_exit_return_value_is_signed(tmp_path: Path) -> None:
    path = tmp_path / "errors.events.bin"
    _write_binary_trace(
        path,
        [
            _futex_enter(1000, 7, 0x1000, 0),
            _futex_exit(1100, 7, -11),  # -EAGAIN
            _futex_enter(2000, 7, 0x1000, 0),
            _futex_exit(2100, 7, -110),  # -ETIMEDOUT
            _end_record(4),
        ],
    )

    return_values = [
        event["futex_exit"]["return_value"]
        for event in read_events(str(path))
        if event["event_type"] == "EVENT_TYPE_FUTEX_EXIT"
    ]

    assert return_values == [-11, -110]


def test_futex_address_uses_all_64_bits(tmp_path: Path) -> None:
    # User-space addresses on x86-64 stay below 2**47, but the field is a full u64
    # and must never be decoded as signed.
    address = 0xFFFF_FFFF_FFFF_F000
    path = tmp_path / "high.events.bin"
    _write_binary_trace(path, [_futex_enter(1000, 7, address, 0), _end_record(1)])

    futex_enter = next(read_events(str(path)))["futex_enter"]

    assert futex_enter["futex_address"] == address


def test_collector_keeps_only_masked_futex_operation_names() -> None:
    # The operations the collector records, by the kernel's numbers.
    assert FUTEX_OPERATION_NAMES == {
        0: "WAIT",
        1: "WAKE",
        3: "REQUEUE",
        4: "CMP_REQUEUE",
        5: "WAKE_OP",
        9: "WAIT_BITSET",
        10: "WAKE_BITSET",
    }


def test_prepare_trace_keeps_target_futex_events_and_drops_outside_ones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed.events.bin"
    _write_binary_trace(
        path,
        [
            _futex_enter(1000, 7, 0x1000, 0),
            _futex_exit(2000, 7, 0),
            _futex_enter(1500, 900, 0x2000, 0, process_id=999),
            _futex_exit(1600, 900, 0, process_id=999),
            _end_record(4),
        ],
    )

    trace = prepare_trace(str(path))

    assert trace.target_pid == PROCESS_ID
    kept_threads = {event["thread_id"] for event in trace.events}
    assert kept_threads == {7}
    assert [event["event_type"] for event in trace.events] == [
        "EVENT_TYPE_FUTEX_ENTER",
        "EVENT_TYPE_FUTEX_EXIT",
    ]
    assert trace.outside_process_event_count == 2


def test_wakeup_inference_ignores_futex_events(tmp_path: Path) -> None:
    sched_events = []
    for i in range(10):
        sched_events.append(waking(1_000_000 + i * 1000, waker=1, woken=2))
        sched_events.append(
            switch(
                1_000_500 + i * 1000, previous_thread=2, next_thread=2, previous_state=1
            )
        )
    futex_events = []
    for i in range(10):
        futex_events.append(_futex_enter_dictionary(1_000_100 + i * 1000, 2, 0x1000, 0))
        futex_events.append(_futex_exit_dictionary(1_000_600 + i * 1000, 2, 0))
        futex_events.append(_futex_enter_dictionary(1_000_050 + i * 1000, 1, 0x1000, 1))
        futex_events.append(_futex_exit_dictionary(1_000_060 + i * 1000, 1, 1))
    settings = InferenceSettings(
        method="wakeup", minimum_count=5, minimum_fraction=0.05
    )

    with_futex_path = tmp_path / "with_futex.jsonl"
    write_jsonl(with_futex_path, sched_events + futex_events + [end_of_trace(60)])
    without_futex_path = tmp_path / "without_futex.jsonl"
    write_jsonl(without_futex_path, sched_events + [end_of_trace(20)])

    with_futex = infer(prepare_trace(str(with_futex_path)), settings)
    without_futex = infer(prepare_trace(str(without_futex_path)), settings)

    assert len(with_futex.edges) == len(without_futex.edges) == 1
    edge = with_futex.edges[0]
    assert (edge.source_thread_id, edge.destination_thread_id) == (1, 2)
    assert edge.wakeup_count == without_futex.edges[0].wakeup_count == 10


def test_summary_reports_futex_addresses_and_return_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "summary.events.bin"
    _write_binary_trace(
        path,
        [
            _futex_enter(1000, 7, 0x1000, 0),
            _futex_exit(2000, 7, -11),
            _futex_enter(3000, 8, 0x1000, 1),
            _futex_exit(3100, 8, 1),
            _end_record(4),
        ],
    )
    monkeypatch.setattr("sys.argv", ["read_events.py", str(path)])

    main()
    output = capsys.readouterr().out

    assert "0x1000 WAIT" in output
    assert "0x1000 WAKE" in output
    assert "-11" in output
    assert "WARNING" not in output


def test_summary_warns_about_futex_exit_without_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "orphan.events.bin"
    _write_binary_trace(path, [_futex_exit(2000, 7, 0), _end_record(1)])
    monkeypatch.setattr("sys.argv", ["read_events.py", str(path)])

    main()

    assert "1 futex exit(s) without an enter" in capsys.readouterr().out
