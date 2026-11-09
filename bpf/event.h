/* bpf/event.h: DRAFT v0, shared by collector (kernel + userspace) and analyzer.
 *
 * Include vmlinux.h (BPF side) or <linux/types.h> (userspace) before this file.
 */
#ifndef EVENT_H
#define EVENT_H

enum event_type {
    EV_SCHED_SWITCH = 1,
    EV_SCHED_WAKING = 2,
    /* EV_FUTEX, EV_IO, EV_COMM: not yet defined */
};

struct event {
    __u64 ts_ns;        /* bpf_ktime_get_ns() */
    __u32 type;         /* enum event_type */
    __u32 cpu;
    __u32 tid;          /* thread that triggered the event */
    __u32 tgid;
    union {
        struct {
            __u32 prev_tid;
            __u32 next_tid;
            __u64 prev_state;
        } sched_switch;
        struct {
            __u32 wakee_tid;    /* tid/tgid above are the waker */
            __u32 wakee_tgid;
        } waking;
    };
};

_Static_assert(sizeof(struct event) == 40, "struct event layout changed");

#endif