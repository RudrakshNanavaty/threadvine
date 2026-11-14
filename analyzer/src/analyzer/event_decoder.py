"""Decode a collector trace into plain dictionaries.

This module is the single owner of the on-disk format. ``bpf/tools/read_events.py``
is a thin shim that imports from here so collector-side summaries stay working.

``read_events(path)`` yields one dictionary per event. Binary layout matches
``bpf/event.h`` / ``docs/event-format.md``; Python keys use the shorter ``_id``
forms (``thread_id``, not ``thread_identifier``), for example::

    {"timestamp_nanoseconds": ..., "event_type": "EVENT_TYPE_SCHEDULER_WAKING",
     "cpu_index": ..., "thread_id": ..., "process_id": ...,
     "scheduler_waking": {"woken_thread_id": ..., "woken_process_id": ...}}

Reserved / unused payload bytes are omitted. Two inputs are accepted:

- Binary ``.events.bin`` from ``collector --output FILE`` (native endianness,
  fixed-size ``struct event`` records after a 32-byte file header).
- ``.jsonl`` with one such dictionary per line (hand-written analyzer tests).

Summary of a trace::

    python3 bpf/tools/read_events.py run.events.bin
"""

import collections
import json
import struct
import sys

# "TRPF" = TRace PFofile; matches bpf/event.h FILE_MAGIC.
FILE_MAGIC = b"TRPF"
# Layout must stay in lockstep with struct event_file_header in bpf/event.h.
# magic, format_version, record_size_bytes, clock_id, target_pid,
# reserved, start_timestamp_nanoseconds
FILE_HEADER = struct.Struct("<4sIIIIIQ")
# Layout must stay in lockstep with the header of struct event in bpf/event.h.
# timestamp_nanoseconds, event_type, cpu_index, thread_id, process_id
EVENT_HEADER = struct.Struct("<QIIII")

PIPE_OR_SOCKET_FIELDS = (
    "inode_number",
    "file_descriptor",
    "byte_count_or_negative_error",
)
# event_type value -> (name, payload member, payload struct format, payload field names).
# Types the collector does not emit yet are still listed so .jsonl fixtures can use them.
EVENT_TYPES = {
    1: (
        "EVENT_TYPE_SCHEDULER_SWITCH",
        "scheduler_switch",
        "<IIQ",
        (
            "previous_thread_id",
            "next_thread_id",
            "previous_thread_state",
        ),
    ),
    2: (
        "EVENT_TYPE_SCHEDULER_WAKING",
        "scheduler_waking",
        "<II",
        ("woken_thread_id", "woken_process_id"),
    ),
    3: (
        "EVENT_TYPE_FUTEX_ENTER",
        "futex_enter",
        "<QI",
        ("futex_address", "futex_operation"),
    ),
    4: ("EVENT_TYPE_FUTEX_EXIT", "futex_exit", "<q", ("return_value",)),
    5: (
        "EVENT_TYPE_PIPE_OR_SOCKET_READ",
        "pipe_or_socket",
        "<Qii",
        PIPE_OR_SOCKET_FIELDS,
    ),
    6: (
        "EVENT_TYPE_PIPE_OR_SOCKET_WRITE",
        "pipe_or_socket",
        "<Qii",
        PIPE_OR_SOCKET_FIELDS,
    ),
    7: ("EVENT_TYPE_THREAD_NAME", "thread_name", None, None),
    0xFFFF: (
        "EVENT_TYPE_END_OF_TRACE",
        "end_of_trace",
        "<QQ",
        ("recorded_event_count", "dropped_event_count"),
    ),
}


def is_binary_trace(path):
    """True for a file written by ``collector --output``, False for anything else (.jsonl)."""
    with open(path, "rb") as trace_file:
        return trace_file.read(len(FILE_MAGIC)) == FILE_MAGIC


def read_file_header(path):
    """Return the binary file's header as a dictionary.

    Raises ``ValueError`` if the magic bytes are wrong or the header is truncated.
    """
    with open(path, "rb") as trace_file:
        raw_header = trace_file.read(FILE_HEADER.size)

    if len(raw_header) < FILE_HEADER.size or raw_header[:4] != FILE_MAGIC:
        raise ValueError(f"{path}: not a collector trace file")

    (
        _,
        format_version,
        record_size_bytes,
        clock_id,
        target_pid,
        _,
        start_timestamp_nanoseconds,
    ) = FILE_HEADER.unpack(raw_header)

    return {
        "format_version": format_version,
        "record_size_bytes": record_size_bytes,
        "clock_id": clock_id,
        "target_pid": target_pid,
        "start_timestamp_nanoseconds": start_timestamp_nanoseconds,
    }


def _decode_record(record):
    """Unpack one fixed-size ``struct event`` into a dictionary."""
    (
        timestamp_nanoseconds,
        event_type,
        cpu_index,
        thread_id,
        process_id,
    ) = EVENT_HEADER.unpack_from(record)
    event = {
        "timestamp_nanoseconds": timestamp_nanoseconds,
        "event_type": event_type,
        "cpu_index": cpu_index,
        "thread_id": thread_id,
        "process_id": process_id,
    }

    known_type = EVENT_TYPES.get(event_type)

    if known_type is None:
        # Forward-compatible: keep the numeric type, leave payload undecoded.
        return event

    name, payload_member, payload_format, field_names = known_type
    payload = record[EVENT_HEADER.size :]
    event["event_type"] = name

    if payload_format is None:
        # thread_name: kernel stores at most 15 chars + NUL in a char[16].
        thread_name = payload[:16].split(b"\0", 1)[0].decode(errors="replace")
        event[payload_member] = {"thread_name": thread_name}

    else:
        event[payload_member] = dict(
            zip(field_names, struct.unpack_from(payload_format, payload))
        )

    return event


def read_events(path):
    """Yield every event in a binary trace or ``.jsonl`` file, in file order.

    File order may not match timestamp order: the collector ring buffer is
    shared across CPUs. Callers that care about time (the analyzer) must sort.
    """
    if not is_binary_trace(path):
        with open(path) as jsonl_file:
            for line in jsonl_file:
                if line.strip():
                    yield json.loads(line)
        return

    record_size_bytes = read_file_header(path)["record_size_bytes"]

    with open(path, "rb") as trace_file:
        trace_file.seek(FILE_HEADER.size)

        while record := trace_file.read(record_size_bytes):
            if len(record) < record_size_bytes:
                raise ValueError(f"{path}: truncated record at end of file")

            yield _decode_record(record)


def main():
    """Print a short summary of a trace file (event counts and top wakeup pairs)."""
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} TRACE_FILE")

    path = sys.argv[1]
    # Counters for each event type and each (waker, woken) wakeup pair.
    event_type_counts = collections.Counter()
    wakeup_pair_counts = collections.Counter()
    end_of_trace = None

    for event in read_events(path):
        event_type_counts[event["event_type"]] += 1
        if event["event_type"] == "EVENT_TYPE_SCHEDULER_WAKING":
            woken = event["scheduler_waking"]["woken_thread_id"]
            wakeup_pair_counts[(event["thread_id"], woken)] += 1

        elif event["event_type"] == "EVENT_TYPE_END_OF_TRACE":
            end_of_trace = event["end_of_trace"]

    if is_binary_trace(path):
        print(read_file_header(path))

    # Print all event types by count.
    for name, count in event_type_counts.most_common():
        print(f"{count:>10}  {name}")

    print(
        end_of_trace
        if end_of_trace
        else "WARNING: no END_OF_TRACE record, trace was cut short"
    )

    print("top wakeups (waker -> woken):")

    for (waker, woken), count in wakeup_pair_counts.most_common(5):
        print(f"{count:>10}  {waker} -> {woken}")


if __name__ == "__main__":
    main()
