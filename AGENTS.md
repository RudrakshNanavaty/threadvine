# Naming conventions

The goal is names a newcomer to systems programming can read without a lookup.
Apply judgment: spell a name out when an abbreviation would make a reader stop and
decode it, and keep the short form when everyone already reads it correctly.

## Rules

1. **Names we choose are spelled out in full.** This covers variables, fields,
   functions, structs, enums, macros, files and CLI flags.
   Example: `thread_identifier`, not `tid`; `previous_thread_state`, not `prev_state`.
2. **Names that come from the kernel or a library keep their original spelling.**
   Do not rename or "improve" them: `sched_switch`, `sched_waking`, `task_struct`,
   `bpf_ktime_get_ns`, `tgid`, `pthread_create`, `clock_gettime`, `argv`, `errno`.
   If one appears in our own code, add a short comment on first use saying what it means.
3. **Names must say what the thing is, not how it is stored.** Prefer
   `woken_thread_identifier` over `target`, and `dropped_event_count` over `drops`.
   Include the unit when a value has one.
4. **Be consistent with the surrounding code.** Match the naming already used in the
   file or module you are editing. Do not rename existing code unless asked.

## Short forms that are fine

Do not expand these; spelling them out makes code harder to read, not easier:

- Loop indices and tiny scopes: `i`, `j`, `n`, and `index` or `count` when clearer.
- Standard unit suffixes in local or internal names: `_ms`, `_us`, `_ns`, `_kb`.
  In shared definitions (for example fields in `bpf/event.h`) spell the unit out,
  as in `timestamp_nanoseconds`.
- Universally understood terms: `cpu`, `id`, `fd` (only next to a `file_descriptor`
  comment or type), `args`, `argv`, `len` for a local string length, `err`,
  `csv`, `src`/`dst` in short copy helpers, `min`/`max`.
- C standard library and POSIX types and macros: `size_t`, `uint64_t`, `pid_t`.

## When unsure

Ask: "Would a reader who has never seen this project understand it in one read?"
If yes, keep it short. If they would have to guess or look it up, spell it out.
Prefer a longer name over a clever one, and never invent a new abbreviation.
