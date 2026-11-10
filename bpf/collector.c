// sched_switch collector loader: prints one line per context switch.
//
// Usage: collector [target_tgid]   (0 or omitted: no filter)
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <linux/types.h>
#include <bpf/libbpf.h>
#include "event.h"
#include "collector.skel.h"

static volatile sig_atomic_t stop;
static unsigned long long events;

static void on_signal(int sig)
{
    (void)sig;
    stop = 1;
}

static int libbpf_print(enum libbpf_print_level level, const char *fmt, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;
    return vfprintf(stderr, fmt, args);
}

/* Short letter for the common task states; raw value otherwise. */
static const char *state_str(__u64 state, char *buf, size_t len)
{
    switch (state) {
    case 0x00: return "R";
    case 0x01: return "S";
    case 0x02: return "D";
    case 0x04: return "T";
    case 0x08: return "t";
    case 0x10: return "X";
    case 0x20: return "Z";
    default:
        snprintf(buf, len, "0x%llx", (unsigned long long)state);
        return buf;
    }
}

static int handle_event(void *ctx, void *data, size_t data_sz)
{
    const struct event *received_event = data;
    char buf[24];

    (void)ctx;
    if (data_sz != sizeof(*received_event))
        return 0;

    events++;
    switch (received_event->event_type) {
    case EVENT_TYPE_SCHEDULER_SWITCH:
        printf("%llu cpu=%u switch %u->%u %s\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index,
               received_event->scheduler_switch.previous_thread_identifier,
               received_event->scheduler_switch.next_thread_identifier,
               state_str(received_event->scheduler_switch.previous_thread_state, buf,
                         sizeof(buf)));
        break;
    case EVENT_TYPE_SCHEDULER_WAKING:
        printf("%llu cpu=%u waking %u->%u tgid=%u\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index, received_event->thread_identifier,
               received_event->scheduler_waking.woken_thread_identifier,
               received_event->scheduler_waking.woken_process_identifier);
        break;
    }
    return 0;
}

int main(int argc, char **argv)
{
    struct collector_bpf *skel;
    struct ring_buffer *rb = NULL;
    unsigned long target = 0;
    int err, ret = 1;

    if (argc > 2) {
        fprintf(stderr, "usage: %s [target_tgid]\n", argv[0]);
        return 1;
    }
    if (argc == 2) {
        char *end;

        errno = 0;
        target = strtoul(argv[1], &end, 10);
        if (errno || *end || end == argv[1] || target > 0xffffffffUL) {
            fprintf(stderr, "invalid tgid: %s\n", argv[1]);
            return 1;
        }
    }

    libbpf_set_print(libbpf_print);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    skel = collector_bpf__open();
    if (!skel) {
        fprintf(stderr, "failed to open BPF skeleton\n");
        return 1;
    }
    /* rodata is frozen at load, so set the filter first. */
    skel->rodata->target_tgid = (__u32)target;
    skel->rodata->self_tgid = (__u32)getpid();

    err = collector_bpf__load(skel);
    if (err) {
        fprintf(stderr, "failed to load BPF skeleton: %d\n", err);
        goto out;
    }

    rb = ring_buffer__new(bpf_map__fd(skel->maps.rb), handle_event, NULL, NULL);
    if (!rb) {
        fprintf(stderr, "failed to create ring buffer\n");
        goto out;
    }

    err = collector_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "failed to attach: %d\n", err);
        goto out;
    }

    while (!stop) {
        err = ring_buffer__poll(rb, 100);
        if (err == -EINTR)
            break;
        if (err < 0) {
            fprintf(stderr, "ring buffer poll failed: %d\n", err);
            goto out;
        }
    }
    ret = 0;

out:
    /* Detach first so the counters are final, then drain what is left. */
    if (rb && ret == 0) {
        collector_bpf__detach(skel);
        ring_buffer__consume(rb);
        fflush(stdout);
        fprintf(stderr, "events: %llu dropped: %llu\n", events,
                (unsigned long long)skel->bss->dropped);
    }
    ring_buffer__free(rb);
    collector_bpf__destroy(skel);
    return ret;
}
