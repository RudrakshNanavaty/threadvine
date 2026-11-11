/* bpf/event.h: DRAFT v1, shared by collector (kernel + userspace) and analyzer.
 *
 * Include vmlinux.h (BPF side) or <linux/types.h> (userspace) before this file.
 *
 * Names we choose are spelled out in full. Names that come from the kernel
 * (tracepoints such as sched_switch, syscalls, helpers) keep the kernel's spelling.
 */
#ifndef EVENT_H
#define EVENT_H

enum event_type {
    EVENT_TYPE_SCHEDULER_SWITCH = 1,
    EVENT_TYPE_SCHEDULER_WAKING = 2,
    /* Defined as part of the contract, not yet emitted by the collector: */
    EVENT_TYPE_FUTEX_ENTER = 3,
    EVENT_TYPE_FUTEX_EXIT = 4,
    EVENT_TYPE_PIPE_OR_SOCKET_READ = 5,
    EVENT_TYPE_PIPE_OR_SOCKET_WRITE = 6,
    EVENT_TYPE_THREAD_NAME = 7,
    /* Written by userspace as the last record of an --output file: */
    EVENT_TYPE_END_OF_TRACE = 0xFFFF,
};

struct event {
    __u64 timestamp_nanoseconds;    /* bpf_ktime_get_ns(), monotonic */
    __u32 event_type;               /* enum event_type */
    __u32 cpu_index;
    __u32 thread_identifier;        /* thread that triggered the event (the actor) */
    __u32 process_identifier;       /* process of that thread */
    union {
        struct {
            __u32 previous_thread_identifier;
            __u32 next_thread_identifier;
            __u64 previous_thread_state;
        } scheduler_switch;
        struct {
            __u32 woken_thread_identifier;      /* thread_identifier above is the waker */
            __u32 woken_process_identifier;
            __u64 reserved;
        } scheduler_waking;
        struct {
            __u64 futex_address;
            __u32 futex_operation;              /* masked with FUTEX_CMD_MASK */
            __u32 reserved;
        } futex_enter;
        struct {
            __s64 return_value;
            __u64 reserved;
        } futex_exit;
        struct {                                /* used by both read and write types */
            __u64 inode_number;
            __s32 file_descriptor;
            __s32 byte_count_or_negative_error;
        } pipe_or_socket;
        struct {
            char thread_name[16];               /* kernel limit: 15 characters + terminator */
        } thread_name;
        struct {
            __u64 recorded_event_count;
            __u64 dropped_event_count;
        } end_of_trace;
    };
};

_Static_assert(sizeof(struct event) == 40, "struct event layout changed");

/* collector --output FILE writes one event_file_header, then struct event records back
 * to back, ending with one EVENT_TYPE_END_OF_TRACE record. Native endianness.
 */
#define EVENT_FILE_MAGIC "TRPF"
#define EVENT_FILE_FORMAT_VERSION 1

struct event_file_header {
    char  magic[4];                     /* EVENT_FILE_MAGIC, no terminator */
    __u32 format_version;               /* EVENT_FILE_FORMAT_VERSION */
    __u32 record_size_bytes;            /* sizeof(struct event) */
    __u32 clock_identifier;             /* 1 = CLOCK_MONOTONIC, the clock of bpf_ktime_get_ns() */
    __u32 target_process_identifier;    /* traced process, 0 = no filter */
    __u32 reserved;
    __u64 start_timestamp_nanoseconds;  /* CLOCK_MONOTONIC just before tracing started */
};

_Static_assert(sizeof(struct event_file_header) == 32, "file header layout changed");

#endif