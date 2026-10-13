# Event Format

The contract between the eBPF collector and the userspace analyzer.
Agree on this before either side diverges.

## Common fields (every record)

- `ts_ns`: timestamp (ns, monotonic)
- `tid`: thread ID
- `tgid`: process ID
- `cpu`: CPU the event occurred on
- `type`: event type

## Event types

TODO: define fields for each.

- `sched_wakeup`: waker TID, wakee TID
- `sched_switch`: prev TID, next TID, prev state
- `futex_enter` / `futex_exit`: futex address, op, return value
- `read` / `write` (pipe/socket): fd, byte count, return value

## Transport

TODO: ring buffer record layout, serialization to disk, dropped-event counter.
