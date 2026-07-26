# Architecture

`reclaim_core` contains the page/domain indexes, intrusive lists, lifecycle transitions, G1 aging, scan pressure, two-phase reclaim, executor contract, statistics and validator. It receives all platform operations through `struct reclaim_platform` stored in each engine instance. There is no mutable global engine state.

The user-space adapter supplies allocator, logical clock, logger and no-op lock operations. The simulator executor supplies deterministic SUCCESS by default and one-shot PUTBACK, ACTIVATE, BUSY, DIRTY, WRITEBACK or UNEVICTABLE outcomes.

## Shadow LRU boundary

The four per-cgroup lists are a complete policy-state model only inside this simulator. In a future kernel integration they are a Shadow index: the kernel global LRU and existing reclaim path remain authoritative for real folio state and execution. The Shadow index must not directly manipulate global kernel LRU nodes.

## Data flow

1. `PAGE_ADD` allocates metadata, indexes the page by page_id, charges it to one domain and appends it to the matching inactive list.
2. `PAGE_ACCESS` changes reference/access metadata only. `AGE_GROUP` or `AGE_ALL` applies deterministic G1 transitions.
3. Reclaim computes priority pressure and anon/file budget, isolates inactive candidates, then passes one batch to the executor.
4. SUCCESS removes metadata; failed outcomes put pages back or activate/mark unevictable. Validator checks the hash index, list membership, lifecycle state and counters.

The folio is indivisible. All targets and statistics use 4 KiB base pages while retaining folio counts; a successful folio larger than the remaining target records overshoot.
