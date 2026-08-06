CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wpedantic -std=c11
LDFLAGS ?=

.PHONY: all clean

all: parp_memory_ballast

parp_memory_ballast: parp_memory_ballast.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f parp_memory_ballast
