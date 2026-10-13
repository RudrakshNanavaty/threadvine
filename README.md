# Discovering Thread Relationships in Multithreaded Applications with eBPF

Team: Karthik Madhusoodanan Panicker, Rudraksh Nehal Nanavaty, Shravya Periyapatna Sham

An eBPF-based profiler that infers relationships among cooperating Linux threads
using scheduler events, selected futex operations, and relevant pipe/socket calls.
We check the inferred graph against a benchmark with known structure, then test
whether a graph-informed CPU-affinity change measurably improves performance.
The existing Linux scheduler is used throughout.

## Environment

- NCSU VCL Linux VM, 4 vCPUs
- Kernel 6.8.0-54-generic, BTF enabled (`/sys/kernel/btf/vmlinux`)
- Administrator access

## Components

| Folder         | Purpose                                                              |
|----------------|----------------------------------------------------------------------|
| `bench/`       | Validation benchmark (pthreads producer-worker-collector, ground truth) |
| `bpf/`         | eBPF collector (kernel programs + userspace loader)                  |
| `analyzer/`    | Userspace analyzer: relationship graph, precision/recall scoring     |
| `experiments/` | Scripts to reproduce overhead and placement experiments              |
| `docs/`        | Proposal, event format spec, design notes, documented limitations    |

## Build

```
make bench
make bpf
make experiments
```

(Targets are placeholders until each component lands.)
