// sched_switch collector loader: writes one JSON object per event
// (JSON lines) to stdout; see docs/event-format.md.
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
static unsigned long long recorded_event_count;
static unsigned long long last_timestamp_nanoseconds;

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

static int handle_event(void *ctx, void *data, size_t data_sz)
{
    const struct event *received_event = data;

    (void)ctx;
    if (data_sz != sizeof(*received_event))
        return 0;

    recorded_event_count++;
    last_timestamp_nanoseconds = received_event->timestamp_nanoseconds;
    switch (received_event->event_type) {
    case EVENT_TYPE_SCHEDULER_SWITCH:
        printf("{\"timestamp_nanoseconds\": %llu, \"event_type\": "
               "\"EVENT_TYPE_SCHEDULER_SWITCH\", \"cpu_index\": %u, "
               "\"thread_identifier\": %u, \"process_identifier\": %u, "
               "\"scheduler_switch\": {\"previous_thread_identifier\": %u, "
               "\"next_thread_identifier\": %u, \"previous_thread_state\": %llu}}\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index, received_event->thread_identifier,
               received_event->process_identifier,
               received_event->scheduler_switch.previous_thread_identifier,
               received_event->scheduler_switch.next_thread_identifier,
               (unsigned long long)received_event->scheduler_switch.previous_thread_state);
        break;
    case EVENT_TYPE_SCHEDULER_WAKING:
        printf("{\"timestamp_nanoseconds\": %llu, \"event_type\": "
               "\"EVENT_TYPE_SCHEDULER_WAKING\", \"cpu_index\": %u, "
               "\"thread_identifier\": %u, \"process_identifier\": %u, "
               "\"scheduler_waking\": {\"woken_thread_identifier\": %u, "
               "\"woken_process_identifier\": %u}}\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index, received_event->thread_identifier,
               received_event->process_identifier,
               received_event->scheduler_waking.woken_thread_identifier,
               received_event->scheduler_waking.woken_process_identifier);
        break;
    default:
        /* Not emitted by the BPF program today; do not count or print it. */
        recorded_event_count--;
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
        unsigned long long dropped_event_count = skel->bss->dropped;

        printf("{\"timestamp_nanoseconds\": %llu, \"event_type\": "
               "\"EVENT_TYPE_END_OF_TRACE\", \"cpu_index\": 0, "
               "\"thread_identifier\": 0, \"process_identifier\": 0, "
               "\"end_of_trace\": {\"recorded_event_count\": %llu, "
               "\"dropped_event_count\": %llu}}\n",
               last_timestamp_nanoseconds, recorded_event_count, dropped_event_count);
        fflush(stdout);
        fprintf(stderr, "events: %llu dropped: %llu\n", recorded_event_count,
                dropped_event_count);
    }
    ring_buffer__free(rb);
    collector_bpf__destroy(skel);
    return ret;
}
