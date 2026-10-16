# Event Format

The contract between the eBPF collector and the userspace analyzer.
Agree on this before either side diverges.

**Status: DRAFT v0.** The source of truth is [`bpf/event.h`](../bpf/event.h);
this file mirrors it and must be updated in the same change.

## Record layout

Every record is a fixed-size `struct event` (40 bytes, 8-byte aligned,
native endianness). A fixed size keeps ring buffer reservation and analyzer
parsing simple. `type` selects which union member is valid, so new event types
do not break parsing of existing ones.

| Field | Type | Meaning |
| --- | --- | --- |
| `ts_ns` | `u64` | `bpf_ktime_get_ns()` (ns, monotonic) |
| `type` | `u32` | `enum event_type` |
| `cpu` | `u32` | CPU the event occurred on |
| `tid` | `u32` | thread that triggered the event |
| `tgid` | `u32` | process ID of that thread |
| payload | union | depends on `type` |

## Event types

| Value | Name | Payload |
| --- | --- | --- |
| 1 | `EV_SCHED_SWITCH` | `prev_tid` (`u32`), `next_tid` (`u32`), `prev_state` (`u64`) |

For `EV_SCHED_SWITCH`, `tid`/`tgid` are those of `prev`, the thread being
switched out, because it is the current task when the tracepoint fires.
`prev_state` is the kernel's raw task state (`0` is runnable/preempted,
`0x1` S, `0x2` D, `0x4` T, `0x8` t, `0x10` X, `0x20` Z; higher bits such as
`0x400` can also appear).

Not yet defined (TODO):

- `sched_wakeup`: waker TID, wakee TID
- `futex_enter` / `futex_exit`: futex address, op, return value
- `read` / `write` (pipe/socket): fd, byte count, return value

## Transport

- A `BPF_MAP_TYPE_RINGBUF` named `rb`, 256 KiB, one `struct event` per record.
- The program reserves a record; if reservation fails the event is lost and
  the global `dropped` counter (`.bss`) is incremented atomically.
- The loader prints `events: N dropped: M` to stderr on exit.
- Filtering happens in the kernel: `target_tgid` (rodata, `0` = no filter)
  keeps only switches where `prev` or `next` belongs to that TGID. The
  collector's own process (`self_tgid`) is always excluded, because reading
  the ring buffer wakes the collector and would otherwise feed back into
  the event stream.

TODO: serialization to disk.
