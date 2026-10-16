# VM Setup

Toolchain for building the eBPF collector on an NCSU VCL VM
(Ubuntu, kernel 6.8.0-54-generic, BTF enabled). VCL images are ephemeral, so
everything lives in one idempotent script.

## Prerequisites

- A VCL VM with `sudo`
- A clone of this repo

## Setup

```
./scripts/setup-vm.sh
```

The script installs packages (clang, llvm, libbpf, libelf, zlib, bpftool via
`linux-tools-$(uname -r)`), mounts tracefs if it is missing, generates `build/vmlinux.h`
from the running kernel's BTF, and finishes with a doctor that prints
`PASS`/`FAIL` for each check. It is safe to rerun.

`build/vmlinux.h` is generated, gitignored, and removed by `make clean`.
Rerun the script to regenerate it.

The script deliberately does not install the `linux-tools-generic` metapackage
unless the exact-kernel tools package is missing: it depends on the latest
kernel's tools, which pulls in a newer kernel image and a grub entry, and a
reboot could then boot a kernel other than 6.8.0-54-generic.

## Doctor checks

| Check | Command | If it fails |
| --- | --- | --- |
| Kernel is `6.8.0-54-generic` | `uname -r` | Wrong VM image. Request the right one; other kernels may work but are unverified. |
| BTF present | `ls /sys/kernel/btf/vmlinux` | Kernel built without BTF; CO-RE will not work on this image. |
| bpftool works | `sudo bpftool version` | Try `sudo apt install bpftool`; as a last resort build `tools/bpf/bpftool` from the kernel source. |
| `sched_switch` tracepoint visible | `sudo ls /sys/kernel/tracing/events/sched/sched_switch` | `sudo mount -t tracefs nodev /sys/kernel/tracing` (the script does this for you). |
| clang has BPF target | `clang -print-targets \| grep bpf` | Reinstall `clang` and `llvm`. |
| `build/vmlinux.h` generated | `grep 'struct task_struct {' build/vmlinux.h` | Check the bpftool failure above, then rerun the script. |
