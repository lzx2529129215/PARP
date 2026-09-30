CLANG ?= clang
CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
UNAME_M := $(shell uname -m)
MULTIARCH ?= $(shell $(CC) -print-multiarch 2>/dev/null)
BPF_ARCH ?= $(if $(filter x86_64 amd64,$(UNAME_M)),x86,$(if $(filter aarch64 arm64,$(UNAME_M)),arm64,$(UNAME_M)))
BPF_CFLAGS ?= -O2 -g -Wall -Werror -target bpf -D__TARGET_ARCH_$(BPF_ARCH)
BPF_INCLUDES := -Iebpf $(if $(MULTIARCH),-I/usr/include/$(MULTIARCH))
USER_LIBS := $(shell pkg-config --cflags --libs libbpf) -lelf -lz

.PHONY: all clean check

all: build/app_fault.bpf.o build/app_fault_collector

build:
	mkdir -p build

build/app_fault.bpf.o: ebpf/app_fault.bpf.c ebpf/app_fault.h | build
	$(CLANG) $(BPF_CFLAGS) $(BPF_INCLUDES) -c $< -o $@

build/app_fault_collector: ebpf/app_fault_user.c ebpf/app_fault.h | build
	$(CC) $(CFLAGS) -Iebpf $< -o $@ $(USER_LIBS)

check: all
	bash -n scripts/*.sh
	python3 -m py_compile scripts/*.py automation/*.py
	python3 -m automation.wps_representative_automation --all --dry-run >/dev/null
	PYTHONPATH=automation python3 -m automation.wps_runner --validate-plan >/dev/null
	./build/app_fault_collector --help >/dev/null

clean:
	rm -f build/app_fault.bpf.o build/app_fault_collector
