.PHONY: all bench bpf run experiments clean
.DELETE_ON_ERROR:

ARCH    := $(shell uname -m | sed 's/x86_64/x86/;s/aarch64/arm64/')
CLANG   ?= clang
BPFTOOL ?= bpftool

all: bench bpf

bench:
	@echo "bench: not implemented yet"

bpf: build/collector

# The repo may live on NFS (krb5), where root cannot read files, so run a
# local copy under sudo. Pass collector arguments with ARGS=...
RUN_DIR ?= /tmp/ebpf-collector-$(shell id -un)

run: bpf
	install -D -m 0755 build/collector $(RUN_DIR)/collector
	sudo $(RUN_DIR)/collector $(ARGS)

experiments:
	@echo "experiments: not implemented yet"

build/vmlinux.h:
	mkdir -p build
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@.tmp
	mv $@.tmp $@

build/collector.bpf.o: bpf/collector.bpf.c bpf/event.h build/vmlinux.h
	$(CLANG) -g -O2 -Wall -Werror -target bpf -D__TARGET_ARCH_$(ARCH) -I build -I bpf -c $< -o $@

build/collector.skel.h: build/collector.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

build/collector: bpf/collector.c bpf/event.h build/collector.skel.h
	$(CC) -g -O2 -Wall -I build -I bpf $< -o $@ -lbpf -lelf -lz

clean:
	rm -rf build
