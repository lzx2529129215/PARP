# PARP Phase2.8B Offline Modeling Design

Phase2.8B consumes only the immutable five-session `RUNTIME_PHASE28_REAL_FRESH`
collection.  It performs bounded per-session parsing, deterministic per-CPU
trace reordering, independently aligned 2s/5s/10s causal windows, sparse
Level-10/100/1000 bitsets, and compressed atomic shards.

The online feature boundary is `foreground_app_id` plus kernel/cgroup state and
causal kernel history.  Operation markers and repeat identifiers are retained
only as supervision metadata.  File identity is retained only for aggregation,
versioning, future-label association and final mapping.

Model selection uses wps_01 for training and wps_02 for validation; wps_03 is
an untouched cross-session and cross-document-size test.  Files uses files_01
for secondary training/internal selection and files_02 for secondary testing.
All probabilities used in causal replay are predictions produced before labels
are loaded for scoring.

The stage is entirely userspace and offline.  It never starts GUI automation,
uses privilege, writes a kernel interface, applies protection, prefetches,
pages out anonymous memory, reboots, or mutates raw input.

