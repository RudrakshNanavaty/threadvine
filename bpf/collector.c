// Collector loader: streams sched_switch, sched_waking and futex events.
//
// Usage: collector [--output FILE] [target_tgid]
//   target_tgid    only events involving this process (0 or omitted: no filter).
//                  tgid is the kernel's name for a process identifier.
//                  With a target, the collector exits on its own when the target exits.
//   --output FILE  (short form -o) write the binary trace file (docs/event-format.md)
//                  instead of text lines on stdout.
#define _GNU_SOURCE
#include <errno.h>
#include <getopt.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <linux/types.h>
#include <bpf/libbpf.h>
#include "event.h"
#include "collector.skel.h"

static volatile sig_atomic_t stop;
static unsigned long long recorded_event_count;
static __u64 last_timestamp_nanoseconds;
static FILE *output_file;       /* binary trace file, or NULL for text on stdout */

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

/* Name of a futex command (kernel spelling, without the FUTEX_ prefix); the raw
 * number for any other value. These are the commands the collector keeps. */
static const char *futex_operation_name(__u32 futex_operation, char *buf, size_t len)
{
    switch (futex_operation) {
    case 0:  return "WAIT";
    case 1:  return "WAKE";
    case 3:  return "REQUEUE";
    case 4:  return "CMP_REQUEUE";
    case 5:  return "WAKE_OP";
    case 9:  return "WAIT_BITSET";
    case 10: return "WAKE_BITSET";
    default:
        snprintf(buf, len, "%u", futex_operation);
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

    recorded_event_count++;
    last_timestamp_nanoseconds = received_event->timestamp_nanoseconds;
    if (output_file)
        return fwrite(received_event, sizeof(*received_event), 1, output_file) == 1 ? 0 : -EIO;

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
    case EVENT_TYPE_FUTEX_ENTER:
        printf("%llu cpu=%u futex_enter thread=%u address=0x%llx operation=%s\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index, received_event->thread_identifier,
               (unsigned long long)received_event->futex_enter.futex_address,
               futex_operation_name(received_event->futex_enter.futex_operation, buf,
                                    sizeof(buf)));
        break;
    case EVENT_TYPE_FUTEX_EXIT:
        printf("%llu cpu=%u futex_exit thread=%u return_value=%lld\n",
               (unsigned long long)received_event->timestamp_nanoseconds,
               received_event->cpu_index, received_event->thread_identifier,
               (long long)received_event->futex_exit.return_value);
        break;
    }
    return 0;
}

/* Called just before attaching, so the start timestamp precedes every event. */
static int write_file_header(__u32 target_process_identifier)
{
    struct event_file_header file_header = {
        .format_version = EVENT_FILE_FORMAT_VERSION,
        .record_size_bytes = sizeof(struct event),
        .clock_identifier = CLOCK_MONOTONIC,
        .target_process_identifier = target_process_identifier,
    };
    struct timespec now;

    memcpy(file_header.magic, EVENT_FILE_MAGIC, sizeof(file_header.magic));
    clock_gettime(CLOCK_MONOTONIC, &now);
    file_header.start_timestamp_nanoseconds =
        (__u64)now.tv_sec * 1000000000ULL + (__u64)now.tv_nsec;
    return fwrite(&file_header, sizeof(file_header), 1, output_file) == 1 ? 0 : -1;
}

/* The last record: a trace without it was cut short. */
static int write_end_of_trace(unsigned long long dropped_event_count)
{
    struct event end_record = {
        .timestamp_nanoseconds = last_timestamp_nanoseconds,
        .event_type = EVENT_TYPE_END_OF_TRACE,
    };

    end_record.end_of_trace.recorded_event_count = recorded_event_count;
    end_record.end_of_trace.dropped_event_count = dropped_event_count;
    return fwrite(&end_record, sizeof(end_record), 1, output_file) == 1 ? 0 : -1;
}

static int usage(const char *program)
{
    fprintf(stderr, "usage: %s [--output FILE] [target_tgid]\n", program);
    return 1;
}

int main(int argc, char **argv)
{
    struct collector_bpf *skel = NULL;
    struct ring_buffer *rb = NULL;
    const char *output_path = NULL;
    unsigned long target = 0;
    int target_process_file_descriptor = -1;
    int option_character, err, ret = 1;
    static const struct option long_options[] = {
        { "output", required_argument, NULL, 'o' },
        { NULL, 0, NULL, 0 },
    };

    while ((option_character = getopt_long(argc, argv, "o:", long_options, NULL)) != -1) {
        if (option_character != 'o')
            return usage(argv[0]);
        output_path = optarg;
    }
    if (argc - optind > 1)
        return usage(argv[0]);
    if (argc - optind == 1) {
        const char *argument = argv[optind];
        char *end;

        errno = 0;
        target = strtoul(argument, &end, 10);
        if (errno || *end || end == argument || target > 0xffffffffUL) {
            fprintf(stderr, "invalid tgid: %s\n", argument);
            return 1;
        }
    }

    /* A pidfd is a file descriptor that refers to a process. It becomes readable
     * when that process exits, so the collector can stop on its own and still
     * write the end-of-trace record. */
    if (target) {
        target_process_file_descriptor = syscall(SYS_pidfd_open, (pid_t)target, 0);
        if (target_process_file_descriptor < 0) {
            fprintf(stderr, "cannot watch process %lu: %s\n", target, strerror(errno));
            return 1;
        }
    }
    if (output_path) {
        output_file = fopen(output_path, "wb");
        if (!output_file) {
            fprintf(stderr, "cannot open %s: %s\n", output_path, strerror(errno));
            goto out;
        }
        setvbuf(output_file, NULL, _IOFBF, 1 << 20);   /* write to disk in 1 MiB chunks */
    }

    libbpf_set_print(libbpf_print);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    skel = collector_bpf__open();
    if (!skel) {
        fprintf(stderr, "failed to open BPF skeleton\n");
        goto out;
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

    if (output_file && write_file_header((__u32)target) != 0) {
        fprintf(stderr, "cannot write %s: %s\n", output_path, strerror(errno));
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
            fprintf(stderr, "ring buffer poll failed: %s\n", strerror(-err));
            goto out;
        }
        if (target_process_file_descriptor >= 0) {
            struct pollfd target_exit_poll = { .fd = target_process_file_descriptor, .events = POLLIN };

            if (poll(&target_exit_poll, 1, 0) > 0)
                break;  /* target exited */
        }
    }
    ret = 0;

out:
    /* Detach first so the counters are final, then drain what is left. */
    if (rb && ret == 0) {
        unsigned long long dropped_event_count;

        collector_bpf__detach(skel);
        ring_buffer__consume(rb);
        dropped_event_count = skel->bss->dropped;
        if (output_file && write_end_of_trace(dropped_event_count) != 0)
            ret = 1;
        fflush(stdout);
        fprintf(stderr, "events: %llu dropped: %llu\n", recorded_event_count,
                dropped_event_count);
    }
    if (output_file) {
        if (fclose(output_file) != 0)
            ret = 1;
        if (ret == 0)
            fprintf(stderr, "wrote %s\n", output_path);
        else
            fprintf(stderr, "%s is incomplete\n", output_path);
    }
    if (target_process_file_descriptor >= 0)
        close(target_process_file_descriptor);
    ring_buffer__free(rb);
    collector_bpf__destroy(skel);
    return ret;
}