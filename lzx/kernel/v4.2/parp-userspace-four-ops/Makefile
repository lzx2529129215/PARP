CC ?= cc
PYTHON ?= python3
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror

.PHONY: all test verify
all: build/libwss_predictor.so build/lookup_example

build:
	mkdir -p build

build/libwss_predictor.so: get_wss_predictor.c get_wss_predictor.h gimp_lookup_data.h | build
	$(CC) $(CFLAGS) -shared -fPIC get_wss_predictor.c -o $@

build/lookup_example: get_wss_predictor.c get_wss_predictor.h gimp_lookup_data.h legacy_example.c | build
	$(CC) $(CFLAGS) get_wss_predictor.c legacy_example.c -o $@

verify:
	$(PYTHON) scripts/verify_sources.py

test: verify
	$(PYTHON) run_operation_tests.py --cc "$(CC)"
