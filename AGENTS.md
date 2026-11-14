# Naming conventions

Use readable names. A reader new to this project should understand a name on first
sight, without decoding an opaque abbreviation.

## What to do

- **Name the thing.** Prefer `woken_thread_id` over `target`, and
  `dropped_event_count` over `drops`. Include the unit when a value has one
  (`timestamp_nanoseconds` in shared schemas; `_ms` / `_us` / `_ns` in locals).
- **Common abbreviations are fine.** Everyone already reads `id`, `pid`, `cpu`,
  `fd`, `args`, `argv`, `err`, `len`, `csv`, `min` / `max`, and unit suffixes.
  Prefer `thread_id` and `process_id` over spelling out `identifier`.
- **Uncommon abbreviations are not.** Do not invent short forms a reader would
  have to look up. `tid` for thread id is opaque — use `thread_id`. `prev_state`
  is opaque — use `previous_thread_state`.
- **Keep related names parallel.** Process and thread often appear together, so
  match their form (`process_id` / `thread_id`). A lone process reference can be
  `pid` or `target_pid` when no thread id sits beside it.
- **Keep kernel and library names as-is.** Do not rename `sched_switch`,
  `sched_waking`, `task_struct`, `bpf_ktime_get_ns`, `tgid`, `pthread_create`,
  `clock_gettime`, `argv`, `errno`, `pid_t`, and the like. On first use in our
  code, add a short comment saying what it means.
- **Match the surrounding code.** Follow names already used in the file or
  module you are editing. Do not rename existing code unless asked.

## When unsure

Ask: "Would a reader who has never seen this project understand it in one read?"
If yes, keep it short. If they would have to guess or look it up, spell it out.
Prefer a longer name over a clever one, and never invent a new abbreviation.
