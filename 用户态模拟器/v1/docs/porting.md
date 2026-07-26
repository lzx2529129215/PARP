# Porting boundary

The portable core depends on per-engine operation tables:

- allocator: alloc/calloc/dealloc;
- clock: a monotonic or logical time source;
- logger: a caller-owned logging sink;
- lock: init/destroy/lock/unlock;
- executor: batch execution feedback.

The v1 user-space adapter uses the C allocator, deterministic logical time, a quiet logger and no-op locks. The executor is a simulator and does not perform real memory-management work.

## Linux

The future Linux adapter must first connect policy queries to the existing global-LRU vmscan path. It must preserve kernel page/folio state as the authority and use existing isolation, putback, reverse mapping, writeback, swap and physical-freeing mechanisms. This project does not provide a kernel module or a replacement reclaim path.

## OpenHarmony

The future OpenHarmony adapter must establish the target kernel version, folio/page structures, cgroup accounting, global LRU call graph and safe lifecycle hook points before mapping ops. It must use platform-native reclaim execution and must not reimplement swap, writeback or unmap in this simulator.
