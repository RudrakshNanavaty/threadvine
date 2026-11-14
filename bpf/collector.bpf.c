// Collector: streams one struct event per context switch, per wakeup, and per
// coordination futex call (enter and exit) through a ring buffer.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include "event.h"

char LICENSE[] SEC("license") = "GPL";

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 16 * 1024 * 1024);  /* 16 MiB: room for bursts while userspace writes */
} rb SEC(".maps");

/* Threads that are inside a futex call whose enter event we recorded, keyed by the
 * value of bpf_get_current_pid_tgid(). The exit probe emits an event only for
 * these, so every recorded exit pairs with a recorded enter.
 * LRU (least recently used entries are evicted when full) so that an entry left
 * behind by a thread that died inside futex() can never fill the map.
 */
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, __u8);
} futex_in_flight SEC(".maps");

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
    struct event *recorded_event;

    if (self_tgid && (prev->tgid == self_tgid || next->tgid == self_tgid))
        return 0;

    if (target_tgid && prev->tgid != target_tgid && next->tgid != target_tgid)
        return 0;

    recorded_event = bpf_ringbuf_reserve(&rb, sizeof(*recorded_event), 0);
    if (!recorded_event) {
        __sync_fetch_and_add(&dropped, 1);
        return 0;
    }

    /* Reserved ring buffer memory is not zeroed; clear it so unused payload
     * bytes never carry stale kernel data into the event stream. */
    __builtin_memset(recorded_event, 0, sizeof(*recorded_event));

    recorded_event->timestamp_nanoseconds = bpf_ktime_get_ns();
    recorded_event->event_type = EVENT_TYPE_SCHEDULER_SWITCH;
    recorded_event->cpu_index = bpf_get_smp_processor_id();
    /* At sched_switch the current task is prev. */
    recorded_event->thread_identifier = prev->pid;
    recorded_event->process_identifier = prev->tgid;
    recorded_event->scheduler_switch.previous_thread_identifier = prev->pid;
    recorded_event->scheduler_switch.next_thread_identifier = next->pid;
    recorded_event->scheduler_switch.previous_thread_state = prev_state;

    bpf_ringbuf_submit(recorded_event, 0);
    return 0;
}

SEC("tp_btf/sched_waking")
int BPF_PROG(handle_waking, struct task_struct *p)
{
    struct event *recorded_event;
    __u64 id = bpf_get_current_pid_tgid();
    __u32 waker_tgid = id >> 32;

    if (self_tgid && (waker_tgid == self_tgid || p->tgid == self_tgid))
        return 0;

    if (target_tgid && waker_tgid != target_tgid && p->tgid != target_tgid)
        return 0;

    recorded_event = bpf_ringbuf_reserve(&rb, sizeof(*recorded_event), 0);
    if (!recorded_event) {
        __sync_fetch_and_add(&dropped, 1);
        return 0;
    }

    /* See handle_switch: clear unused payload bytes. */
    __builtin_memset(recorded_event, 0, sizeof(*recorded_event));

    recorded_event->timestamp_nanoseconds = bpf_ktime_get_ns();
    recorded_event->event_type = EVENT_TYPE_SCHEDULER_WAKING;
    recorded_event->cpu_index = bpf_get_smp_processor_id();
    /* sched_waking runs in the waker's context, so the current task is the waker. */
    recorded_event->thread_identifier = (__u32)id;
    recorded_event->process_identifier = waker_tgid;
    recorded_event->scheduler_waking.woken_thread_identifier = p->pid;
    recorded_event->scheduler_waking.woken_process_identifier = p->tgid;

    bpf_ringbuf_submit(recorded_event, 0);
    return 0;
}

/* Futex constants from the kernel's uapi header <linux/futex.h> (kernel spelling).
 * Defined here because vmlinux.h carries types, not macros.
 */
#define FUTEX_WAIT 0
#define FUTEX_WAKE 1
#define FUTEX_REQUEUE 3
#define FUTEX_CMP_REQUEUE 4
#define FUTEX_WAKE_OP 5
#define FUTEX_WAIT_BITSET 9
#define FUTEX_WAKE_BITSET 10
#define FUTEX_PRIVATE_FLAG 128
#define FUTEX_CLOCK_REALTIME 256
#define FUTEX_CMD_MASK (~(FUTEX_PRIVATE_FLAG | FUTEX_CLOCK_REALTIME))

/* Which futex commands we keep: the ones that block a thread on an address or
 * wake threads blocked on one. These are what ordinary mutexes, condition
 * variables, semaphores and joins use, so they carry the synchronization
 * evidence the analyzer needs.
 *   kept:    FUTEX_WAIT, FUTEX_WAIT_BITSET            (block on an address)
 *            FUTEX_WAKE, FUTEX_WAKE_BITSET            (wake blocked threads)
 *            FUTEX_REQUEUE, FUTEX_CMP_REQUEUE         (move waiters to another
 *                                                      address; condition variables)
 *            FUTEX_WAKE_OP                            (wake on one address after an
 *                                                      atomic operation on another)
 *   skipped: the priority-inheritance family (FUTEX_LOCK_PI, FUTEX_LOCK_PI2,
 *            FUTEX_TRYLOCK_PI, FUTEX_UNLOCK_PI, FUTEX_WAIT_REQUEUE_PI,
 *            FUTEX_CMP_REQUEUE_PI), used only by priority-inheritance mutexes,
 *            which the benchmark and the target applications do not use;
 *            FUTEX_FD, removed from the kernel long ago.
 */
static __always_inline bool is_coordination_futex_operation(__u32 futex_operation)
{
    switch (futex_operation) {
    case FUTEX_WAIT:
    case FUTEX_WAKE:
    case FUTEX_REQUEUE:
    case FUTEX_CMP_REQUEUE:
    case FUTEX_WAKE_OP:
    case FUTEX_WAIT_BITSET:
    case FUTEX_WAKE_BITSET:
        return true;
    default:
        return false;
    }
}

/* Reserve and fill the common header of a futex event for the current thread.
 * Returns NULL, and counts a dropped event, when the ring buffer is full.
 */
static __always_inline struct event *reserve_futex_event(__u32 event_type, __u64 id)
{
    struct event *recorded_event = bpf_ringbuf_reserve(&rb, sizeof(*recorded_event), 0);

    if (!recorded_event) {
        __sync_fetch_and_add(&dropped, 1);
        return NULL;
    }

    /* See handle_switch: clear unused payload bytes. */
    __builtin_memset(recorded_event, 0, sizeof(*recorded_event));

    recorded_event->timestamp_nanoseconds = bpf_ktime_get_ns();
    recorded_event->event_type = event_type;
    recorded_event->cpu_index = bpf_get_smp_processor_id();
    /* Both futex tracepoints run in the context of the thread making the call. */
    recorded_event->thread_identifier = (__u32)id;
    recorded_event->process_identifier = id >> 32;
    return recorded_event;
}

/* Arguments of futex(uaddr, op, ...): args[0] is uaddr, args[1] is op. */
SEC("tracepoint/syscalls/sys_enter_futex")
int handle_futex_enter(struct trace_event_raw_sys_enter *context)
{
    struct event *recorded_event;
    __u64 id = bpf_get_current_pid_tgid();
    __u32 tgid = id >> 32;
    __u32 futex_operation = (__u32)context->args[1] & FUTEX_CMD_MASK;
    __u8 in_flight = 1;

    if (self_tgid && tgid == self_tgid)
        return 0;

    if (target_tgid && tgid != target_tgid)
        return 0;

    if (!is_coordination_futex_operation(futex_operation)) {
        /* Forget any stale entry left by a thread that died inside futex() and
         * whose identifier was reused, so its exit is not reported. */
        bpf_map_delete_elem(&futex_in_flight, &id);
        return 0;
    }

    recorded_event = reserve_futex_event(EVENT_TYPE_FUTEX_ENTER, id);
    if (!recorded_event) {
        /* No enter was recorded, so make sure the exit is not either. */
        bpf_map_delete_elem(&futex_in_flight, &id);
        return 0;
    }

    recorded_event->futex_enter.futex_address = context->args[0];
    recorded_event->futex_enter.futex_operation = futex_operation;
    bpf_ringbuf_submit(recorded_event, 0);

    bpf_map_update_elem(&futex_in_flight, &id, &in_flight, BPF_ANY);
    return 0;
}

SEC("tracepoint/syscalls/sys_exit_futex")
int handle_futex_exit(struct trace_event_raw_sys_exit *context)
{
    struct event *recorded_event;
    __u64 id = bpf_get_current_pid_tgid();

    /* Only exits of calls whose enter we recorded. This also covers a call that
     * began before tracing started and the target filter. */
    if (!bpf_map_lookup_elem(&futex_in_flight, &id))
        return 0;
    bpf_map_delete_elem(&futex_in_flight, &id);

    recorded_event = reserve_futex_event(EVENT_TYPE_FUTEX_EXIT, id);
    if (!recorded_event)
        return 0;

    recorded_event->futex_exit.return_value = context->ret;
    bpf_ringbuf_submit(recorded_event, 0);
    return 0;
}
