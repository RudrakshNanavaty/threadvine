# Event Format

The contract between the eBPF collector and the userspace analyzer.
Agree on this before either side diverges.

**Status: DRAFT v1.** The source of truth is [`bpf/event.h`](../bpf/event.h);
this file mirrors it and must be updated in the same change.

Names we choose are spelled out in full. Names that come from the kernel
(tracepoints such as `sched_switch`, syscalls, helpers) keep the kernel's
spelling.

## Record layout

Every record is a fixed-size `struct event` (40 bytes, 8-byte aligned,
native endianness): a 24-byte header followed by a 16-byte payload union. A
fixed size keeps ring buffer reservation and analyzer parsing simple.
`event_type` selects which union member is valid, so new event types do not
break parsing of existing ones. Unused payload bytes (`reserved`) are zero.

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp_nanoseconds` | `u64` | `bpf_ktime_get_ns()` (ns, monotonic) |
| `event_type` | `u32` | `enum event_type` |
| `cpu_index` | `u32` | CPU the event occurred on |
| `thread_identifier` | `u32` | thread that triggered the event (the actor) |
| `process_identifier` | `u32` | process of that thread |
| payload | union | depends on `event_type` |

The meaning of "the actor" depends on the event type: the thread switched out,
the waker, the thread calling futex, the reader or writer, and so on.

## Event types

| Value | Name | Payload member | Fields | Emitted |
| --- | --- | --- | --- | --- |
| 1 | `EVENT_TYPE_SCHEDULER_SWITCH` | `scheduler_switch` | `previous_thread_identifier` (`u32`), `next_thread_identifier` (`u32`), `previous_thread_state` (`u64`) | yes |
| 2 | `EVENT_TYPE_SCHEDULER_WAKING` | `scheduler_waking` | `woken_thread_identifier` (`u32`), `woken_process_identifier` (`u32`) | yes |
| 3 | `EVENT_TYPE_FUTEX_ENTER` | `futex_enter` | `futex_address` (`u64`), `futex_operation` (`u32`) | not yet |
| 4 | `EVENT_TYPE_FUTEX_EXIT` | `futex_exit` | `return_value` (`s64`) | not yet |
| 5 | `EVENT_TYPE_PIPE_OR_SOCKET_READ` | `pipe_or_socket` | `inode_number` (`u64`), `file_descriptor` (`s32`), `byte_count_or_negative_error` (`s32`) | not yet |
| 6 | `EVENT_TYPE_PIPE_OR_SOCKET_WRITE` | `pipe_or_socket` | same as read | not yet |
| 7 | `EVENT_TYPE_THREAD_NAME` | `thread_name` | `thread_name` (`char[16]`) | not yet |
| 0xFFFF | `EVENT_TYPE_END_OF_TRACE` | `end_of_trace` | `recorded_event_count` (`u64`), `dropped_event_count` (`u64`) | yes (`--output` file only) |

Types marked "not yet" are part of the contract so the analyzer can be built
against them, but the collector does not produce them yet.

### Scheduler switch

`thread_identifier` / `process_identifier` are those of the previous thread,
the one being switched out, because it is the current task when the tracepoint
fires. `previous_thread_state` is the kernel's raw task state (`0` is
runnable/preempted, `0x1` S, `0x2` D, `0x4` T, `0x8` t, `0x10` X, `0x20` Z;
higher bits such as `0x400` can also appear).

### Scheduler waking

Recorded from `sched_waking`, which fires in the waker's context, so
`thread_identifier` / `process_identifier` are the waker's. The woken thread
and its process are in the payload. The analyzer computes wakeup-to-execution
delay from this timestamp to the woken thread's next switch-in.
`sched_wakeup` is deliberately not used: for remote wakeups it can fire on the
woken thread's CPU, where the current task is not the waker.

### Futex enter and exit

`futex_operation` is the futex command masked with `FUTEX_CMD_MASK`.
`futex_address` identifies the same lock or condition variable across threads,
since threads share one address space. An exit pairs with the enter from the
same `thread_identifier`.

### Pipe or socket read and write

Direction comes from the event type. `byte_count_or_negative_error` is the
byte count, or a negative error number, packed into 32 bits. `inode_number`
matches the two ends of a pipe; it does not say whether the object is a pipe
or a socket, and the two ends of a socket have different inodes.

### Thread name

Emitted once per thread. The kernel stores at most 15 characters plus a
terminator, so longer names are truncated.

### End of trace

The last record of a file, written by the userspace loader (not the BPF
program) after the final drain. `recorded_event_count` excludes the end record
itself, and `dropped_event_count` is the `dropped` counter. Its
`timestamp_nanoseconds` is that of the last recorded event; the other header
fields are 0. A file without it was cut short.

## Ordering

The ring buffer is shared by all CPUs, so records can arrive slightly out of
timestamp order. The analyzer should sort by `timestamp_nanoseconds` before
analysis.

## Transport

- A `BPF_MAP_TYPE_RINGBUF` named `rb`, 16 MiB, one `struct event` per record.
- The program reserves a record and zeroes it; if reservation fails the event
  is lost and the global `dropped` counter (`.bss`) is incremented atomically.
- The loader prints `events: N dropped: M` to stderr on exit.
- Filtering happens in the kernel: `target_tgid` (rodata, `0` = no filter)
  keeps only events where the actor or the other thread involved (next thread
  on a switch, woken thread on a wakeup) belongs to that process. The
  collector's own process (`self_tgid`) is always excluded, because reading
  the ring buffer wakes the collector and would otherwise feed back into
  the event stream.
- With a target, the collector exits on its own once the target process exits
  (it watches a pidfd, a file descriptor that refers to the process), drains
  the ring buffer and finishes the file.

## File format

`collector --output FILE [target_tgid]` (short form `-o`) writes a binary file,
native endianness:

1. One 32-byte `struct event_file_header` (see `bpf/event.h`):

   | Field | Type | Meaning |
   | --- | --- | --- |
   | `magic` | `char[4]` | `TRPF`, no terminator |
   | `format_version` | `u32` | `1`; bumped when `struct event` changes |
   | `record_size_bytes` | `u32` | `sizeof(struct event)`, currently 40 |
   | `clock_identifier` | `u32` | `1` = `CLOCK_MONOTONIC`, the same clock as the benchmark's CSV timestamps |
   | `target_process_identifier` | `u32` | traced process, `0` = no filter |
   | `reserved` | `u32` | zero |
   | `start_timestamp_nanoseconds` | `u64` | `CLOCK_MONOTONIC` just before tracing started |

2. `struct event` records back to back, in the order they left the ring buffer.
3. One `EVENT_TYPE_END_OF_TRACE` record on a clean exit.

Without `--output`, the collector prints one text line per event to stdout, for
debugging only.

Example run against the benchmark:

```
./build/pipeline -d 5 ... -o traced_run & make -s run ARGS="--output traced_run.events.bin $!"
```

`bpf/tools/read_events.py` reads the file: `read_events(path)` yields each
event as a dictionary with the field names above (payload under the union
member's name, reserved fields omitted). It also accepts a `.jsonl` file of the
same dictionaries, for hand-written analyzer tests. Run it directly for a
summary: `python3 bpf/tools/read_events.py traced_run.events.bin`.