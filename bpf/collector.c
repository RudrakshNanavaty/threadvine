// Pass A loader: load, attach, wait for SIGINT.
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <unistd.h>
#include <bpf/libbpf.h>
#include "collector.skel.h"

static volatile sig_atomic_t stop;

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

int main(void)
{
    struct collector_bpf *skel;
    int err;

    libbpf_set_print(libbpf_print);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    skel = collector_bpf__open_and_load();
    if (!skel) {
        fprintf(stderr, "failed to open and load BPF skeleton\n");
        return 1;
    }

    err = collector_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "failed to attach: %d\n", err);
        collector_bpf__destroy(skel);
        return 1;
    }

    fprintf(stderr, "attached; see /sys/kernel/tracing/trace_pipe (Ctrl-C to exit)\n");
    while (!stop)
        pause();

    collector_bpf__destroy(skel);
    return 0;
}
