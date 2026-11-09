#!/usr/bin/env bash
# Idempotent toolchain setup for the eBPF collector on a fresh VCL VM.
# Installs packages, mounts tracefs if needed, generates build/vmlinux.h,
# then runs a doctor that checks everything. Safe to rerun.
set -euo pipefail

EXPECTED_KERNEL="6.8.0-54-generic"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build"
TRACING=/sys/kernel/tracing

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- Preflight ---------------------------------------------------------------
command -v apt-get >/dev/null || die "apt-get not found; this script targets Ubuntu"
command -v sudo >/dev/null || die "sudo not found"

# --- Install packages --------------------------------------------------------
log "Installing packages"
sudo apt-get update

PACKAGES=(
    build-essential clang llvm libbpf-dev libelf-dev zlib1g-dev pkg-config
    linux-tools-common
)
# linux-tools-generic depends on the *latest* kernel's tools and drags in a newer
# kernel image plus a grub entry, so a reboot could leave us off the pinned
# kernel. Only fall back to it when the running kernel's package is unavailable.
if apt-cache show "linux-tools-$(uname -r)" >/dev/null 2>&1; then
    PACKAGES+=("linux-tools-$(uname -r)")
else
    warn "linux-tools-$(uname -r) not in apt; falling back to linux-tools-generic"
    warn "  (this may install a newer kernel; check 'uname -r' after a reboot)"
    PACKAGES+=(linux-tools-generic)
fi
sudo apt-get install -y "${PACKAGES[@]}"

if ! sudo bpftool version >/dev/null 2>&1; then
    log "bpftool not working; trying the bpftool package"
    sudo apt-get install -y bpftool || warn "apt package 'bpftool' unavailable"
fi
if ! sudo bpftool version >/dev/null 2>&1; then
    warn "bpftool still not working. Last resort: build it from the kernel source"
    warn "  (tools/bpf/bpftool: 'make' then 'sudo make install')"
fi

# --- Tracefs -----------------------------------------------------------------
if ! sudo test -d "$TRACING/events/sched/sched_switch"; then
    log "Mounting tracefs at $TRACING"
    sudo mount -t tracefs nodev "$TRACING" || warn "tracefs mount failed"
fi

# --- vmlinux.h ---------------------------------------------------------------
log "Generating $BUILD_DIR/vmlinux.h"
mkdir -p "$BUILD_DIR"
tmp="$(mktemp "$BUILD_DIR/vmlinux.h.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
if sudo bpftool btf dump file /sys/kernel/btf/vmlinux format c > "$tmp"; then
    mv "$tmp" "$BUILD_DIR/vmlinux.h"
else
    warn "bpftool btf dump failed; vmlinux.h not updated"
fi

# --- Doctor ------------------------------------------------------------------
log "Doctor"
failures=0
check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf '  PASS  %s\n' "$name"
    else
        printf '  FAIL  %s\n' "$name"
        failures=$((failures + 1))
    fi
}

check "Kernel is $EXPECTED_KERNEL" test "$(uname -r)" = "$EXPECTED_KERNEL"
check "BTF present (/sys/kernel/btf/vmlinux)" test -r /sys/kernel/btf/vmlinux
check "bpftool works" sudo bpftool version
check "sched_switch tracepoint visible" sudo test -f "$TRACING/events/sched/sched_switch/format"
check "clang has BPF target" bash -c 'clang -print-targets | grep -q bpf'
check "build/vmlinux.h generated" bash -c "test -s '$BUILD_DIR/vmlinux.h' && grep -q 'struct task_struct {' '$BUILD_DIR/vmlinux.h'"

if [ "$failures" -ne 0 ]; then
    die "$failures check(s) failed; see docs/setup.md"
fi
log "All checks passed"
