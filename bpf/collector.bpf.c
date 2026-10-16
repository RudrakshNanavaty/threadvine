// Pass A: prove the program attaches. Output goes to /sys/kernel/tracing/trace_pipe.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

SEC("tp_btf/sched_switch")
int BPF_PROG(handle_switch, bool preempt, struct task_struct *prev,
             struct task_struct *next, unsigned int prev_state)
{
    bpf_printk("%d -> %d", prev->pid, next->pid);
    return 0;
}
