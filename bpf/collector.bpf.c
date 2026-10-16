// sched_switch collector: streams one struct event per context switch through a ring buffer.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "event.h"

char LICENSE[] SEC("license") = "GPL";

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} rb SEC(".maps");

/* Set from userspace before load. 0 means no filter. */
const volatile __u32 target_tgid = 0;

/* The collector's own TGID, set from userspace before load. Reading the ring
 * buffer wakes the collector, which causes a switch, which would emit another
 * event: skip events involving ourselves to break that feedback loop.
 */
const volatile __u32 self_tgid = 0;

/* Events lost because the ring buffer was full. */
volatile __u64 dropped = 0;

SEC("tp_btf/sched_switch")
int BPF_PROG(handle_switch, bool preempt, struct task_struct *prev,
             struct task_struct *next, unsigned int prev_state)
{
    struct event *e;

    if (self_tgid && (prev->tgid == self_tgid || next->tgid == self_tgid))
        return 0;

    if (target_tgid && prev->tgid != target_tgid && next->tgid != target_tgid)
        return 0;

    e = bpf_ringbuf_reserve(&rb, sizeof(*e), 0);
    if (!e) {
        __sync_fetch_and_add(&dropped, 1);
        return 0;
    }

    e->ts_ns = bpf_ktime_get_ns();
    e->type = EV_SCHED_SWITCH;
    e->cpu = bpf_get_smp_processor_id();
    /* At sched_switch the current task is prev. */
    e->tid = prev->pid;
    e->tgid = prev->tgid;
    e->sched_switch.prev_tid = prev->pid;
    e->sched_switch.next_tid = next->pid;
    e->sched_switch.prev_state = prev_state;

    bpf_ringbuf_submit(e, 0);
    return 0;
}
