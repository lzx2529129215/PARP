# Shadow per-cgroup LRU Simulator Design

**Project:** `myself-kswapd`
**Date:** 2026-07-26
**Status:** Approved design, implementation not started
**Primary language:** C11
**Build system:** CMake + CTest

## 1. Purpose

This project builds a portable, deterministic, single-threaded C simulator for a prediction-driven memory-reclaim engine. The first version validates the data structures and state transitions required to maintain a **Shadow per-cgroup LRU** beside a kernel that may only expose a global LRU.

The simulator does **not** reclaim real physical memory. It models page/folio metadata, cgroup ownership, active/inactive LRU state, aging, reclaim pressure, candidate isolation, execution outcomes, putback, statistics, and consistency checking.

The long-term integration path is:

1. Validate the portable core in user space.
2. Integrate the same decision logic into Linux as a real-kernel validation platform.
3. Adapt the platform and executor layers to OpenHarmony.
4. Add prediction-driven cgroup protection and reclaim weighting.

## 2. Core Architectural Principle

The design separates three roles:

- **Kernel global LRU:** authoritative source of real folio state and the execution structure used by the kernel reclaim path.
- **Shadow per-cgroup LRU:** auxiliary cgroup-indexed policy view used to classify, protect, rank, and recommend reclaim candidates.
- **Kernel reclaim executor:** existing kernel mechanisms that isolate, unmap, write back, swap out, remove mappings, update accounting, and free real memory.

The Shadow LRU does not own real folio lifetime and must never be treated as authoritative over the kernel. In the first conservative kernel integration, the kernel follows its original global-LRU scan order and queries Shadow metadata to allow, defer, protect, or lower the priority of the current folio.

## 3. Scope

### 3.1 First implementation phase

The first phase implements:

- C11 portable core.
- CMake and CTest.
- Single-threaded deterministic event execution.
- Dynamic page and cgroup metadata.
- Instance-level platform operation tables.
- Page and cgroup hash indexes.
- Four Shadow LRU lists per cgroup:
  - inactive anonymous,
  - active anonymous,
  - inactive file,
  - active file.
- Simplified G1 aging policy.
- `AGE_GROUP` and `AGE_ALL`.
- `RECLAIM_GROUP` and `RECLAIM_ALL`.
- Linux-like priority-based scan pressure baseline.
- Configurable `swappiness` and `swap_enabled`.
- Folio `order`, base-page accounting, and overshoot accounting.
- Two-phase isolate/execute/putback flow.
- User-space simulator executor with default success and one-shot outcome injection.
- Per-event validation in test mode.
- Text trace replay and direct C test APIs.
- Unit, integration, scenario, failure-injection, and determinism tests.

### 3.2 Explicitly excluded from the first phase

The first phase does not implement:

- Real physical memory reclamation.
- Linux kernel modules or kernel patches.
- OpenHarmony kernel modifications.
- Real swap, writeback, reverse mapping, page-table updates, or unmapping.
- Multithreaded or concurrent access.
- Full Linux active/inactive aging behavior.
- MGLRU.
- LSTM, Markov, or prediction logic.
- `memory.min` or `memory.low` propagation.
- NUMA nodes or zones.
- Shadow-to-kernel lifecycle synchronization hooks.

## 4. Repository Layout

```text
myself-kswapd/
├── CMakeLists.txt
├── README.md
├── LICENSE
├── include/
│   └── myself_kswapd/
│       ├── engine.h
│       ├── event.h
│       ├── types.h
│       ├── error.h
│       ├── platform.h
│       ├── policy.h
│       ├── executor.h
│       ├── stats.h
│       └── validator.h
├── src/
│   ├── core/
│   │   ├── engine.c
│   │   ├── page.c
│   │   ├── domain.c
│   │   ├── hash.c
│   │   ├── list.c
│   │   ├── lru.c
│   │   ├── aging_g1.c
│   │   ├── scan_pressure.c
│   │   ├── reclaim.c
│   │   ├── stats.c
│   │   └── validator.c
│   └── simulator/
│       ├── main.c
│       ├── event_parser.c
│       ├── event_runner.c
│       ├── userspace_platform.c
│       └── simulator_executor.c
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── scenarios/
│   └── test_support/
├── adapters/
│   ├── linux/
│   │   └── README.md
│   └── openharmony/
│       └── README.md
└── docs/
    ├── architecture.md
    ├── event-format.md
    ├── porting.md
    └── superpowers/
        ├── specs/
        └── plans/
```

## 5. Build and Execution Model

### 5.1 Build outputs

CMake creates:

- `reclaim_core`: portable static library.
- `reclaim_simulator`: text-trace replay executable.
- `reclaim_tests`: CTest-registered test targets.

The project has no third-party runtime dependency.

### 5.2 Concurrency model

Version 1 is single-threaded and deterministic. All events are processed sequentially. The core contains no mutable global state. Every public operation receives a `reclaim_engine *`.

Lock operations are abstracted but implemented as no-ops in the user-space simulator. A later kernel or concurrent version may map the same lock interface to spinlocks, mutexes, or another synchronization mechanism.

## 6. Platform Abstraction

The portable core never directly calls `malloc`, `free`, `printf`, platform clocks, pthread primitives, or kernel functions. Platform dependencies are injected into each engine instance through operation tables.

Representative interfaces:

```c
struct reclaim_allocator_ops {
    void *(*alloc)(void *context, size_t size);
    void *(*calloc)(void *context, size_t count, size_t size);
    void (*dealloc)(void *context, void *pointer);
};

struct reclaim_clock_ops {
    uint64_t (*get_time_ns)(void *context);
};

struct reclaim_log_ops {
    void (*log)(void *context, int level, const char *message);
};

struct reclaim_lock_ops {
    int (*init)(void *context, void **lock);
    void (*destroy)(void *context, void *lock);
    void (*lock)(void *context, void *lock);
    void (*unlock)(void *context, void *lock);
};
```

The user-space platform maps these operations to the C runtime. Linux and OpenHarmony adapters will later provide kernel implementations. Operation tables are stored per engine instance rather than in a mutable global registry.

## 7. Main Data Structures

### 7.1 Engine

```c
struct reclaim_engine {
    struct reclaim_platform platform;
    struct reclaim_engine_config config;

    struct reclaim_page_table page_table;
    struct reclaim_domain_table domain_table;

    const struct reclaim_aging_ops *aging_ops;
    const struct reclaim_domain_selector_ops *selector_ops;
    const struct reclaim_executor_ops *executor_ops;

    struct reclaim_engine_stats stats;
    uint64_t event_seq;
};
```

The engine coordinates modules but does not contain all policy logic.

### 7.2 Domain

Each cgroup has one domain:

```c
struct reclaim_domain {
    uint64_t cgroup_id;

    struct reclaim_lru_list inactive_anon;
    struct reclaim_lru_list active_anon;
    struct reclaim_lru_list inactive_file;
    struct reclaim_lru_list active_file;

    struct reclaim_domain_config config;
    struct reclaim_domain_stats stats;

    struct reclaim_domain *hash_next;
};
```

Each domain contains exactly four ordinary reclaim lists. Unevictable folios are tracked by lifecycle state and accounting but are not placed back into the ordinary four-list set. The domain table maintains both a hash index for lookup and a secondary intrusive list sorted by `cgroup_id`; global aging and reclaim traverse the sorted list rather than hash buckets.

### 7.3 Folio metadata

One `reclaim_page` object represents one indivisible folio:

```c
struct reclaim_page {
    uint64_t page_id;

    uint64_t charge_cgroup_id;
    uint64_t last_access_cgroup_id;

    enum reclaim_page_type type;
    enum reclaim_page_state state;
    enum reclaim_lru_kind lru_kind;

    uint32_t order;
    uint32_t flags;

    uint64_t last_access_seq;
    uint64_t last_age_seq;
    uint32_t access_count;

    bool referenced;
    bool shared;

    enum reclaim_sim_outcome next_sim_outcome;

    struct reclaim_list_node lru_node;
    struct reclaim_page *hash_next;
};
```

The number of 4 KiB base pages represented by a folio is:

```c
1ULL << order
```

The first version never splits a folio.

## 8. Ownership Semantics

Each folio has exactly one memory-accounting owner:

```text
charge_cgroup_id
```

The cgroup that creates or charges the folio owns its Shadow LRU membership. Later access by another cgroup updates:

- `last_access_cgroup_id`,
- `shared`,
- access statistics,

but does not change `charge_cgroup_id` and does not move the folio to another domain.

Only an explicit recharge operation may move a folio from one cgroup's Shadow LRU to another. A physical page migration is distinct from a cgroup recharge and preserves ownership.

## 9. Page Lifecycle

```c
enum reclaim_page_state {
    RECLAIM_PAGE_NEW,
    RECLAIM_PAGE_ON_LRU,
    RECLAIM_PAGE_ISOLATED,
    RECLAIM_PAGE_UNEVICTABLE,
};
```

Legal transitions:

```text
PAGE_ADD:
NEW -> ON_LRU

candidate isolation:
ON_LRU -> ISOLATED

putback:
ISOLATED -> ON_LRU

activation after execution:
ISOLATED -> ON_LRU, placed on the active list

unevictable outcome:
ISOLATED -> UNEVICTABLE

successful reclaim:
ISOLATED -> removed from indexes and metadata freed
```

A reclaimed object is not retained in a long-lived `RECLAIMED` state.

## 10. Aging Policy

Version 1 implements `G1`, a deterministic simulator policy, not a full reproduction of Linux page aging.

### 10.1 Access

`PAGE_ACCESS` performs:

```text
referenced = true
last_access_seq = current event sequence
last_access_cgroup_id = accessing cgroup
access_count += 1
shared = true when accessing cgroup differs from charge cgroup
```

The access event does not immediately modify LRU membership.

### 10.2 Aging

Supported scopes:

- `AGE_GROUP <cgroup_id>`
- `AGE_ALL`

`AGE_ALL` processes domains in ascending `cgroup_id` order.

G1 transitions:

| Current list | `referenced` | Result |
|---|---:|---|
| inactive | true | move to active tail |
| inactive | false | remain in place |
| active | true | move to active tail |
| active | false | move to inactive tail |

After aging, `referenced` is cleared and `last_age_seq` is updated.

The aging logic is accessed through `reclaim_aging_ops` so a more Linux-like policy or MGLRU can replace it later without changing engine, hash, domain, executor, or parser code.

## 11. Reclaim Requests

Supported requests:

```text
RECLAIM_GROUP <cgroup_id> <target_pages>
RECLAIM_ALL <target_pages>
```

`target_pages` always means 4 KiB base pages.

### 11.1 Directed reclaim

`RECLAIM_GROUP` scans only the requested domain.

### 11.2 Global reclaim

`RECLAIM_ALL` uses a Linux-inspired dynamic pressure baseline rather than a precomputed static cgroup quota table.

For each priority level:

1. Traverse domains in ascending `cgroup_id` order.
2. Compute effective reclaimable LRU size.
3. Compute scan pressure using:

   ```c
   scan_pages = effective_lru_pages >> priority;
   ```

4. If reclaimable pages exist but the result is zero, attempt at least one base page.
5. Split the scan budget between anonymous and file folios.
6. Scan in bounded batches.
7. Accumulate scanned, isolated, reclaimed, activated, and putback statistics.
8. Stop immediately when the target is reached.
9. Otherwise decrease priority and repeat.

Default pressure configuration:

```c
struct reclaim_pressure_config {
    uint32_t default_priority;    /* 12 */
    uint32_t minimum_priority;    /* 0 */
    uint32_t scan_batch_pages;    /* 32 */
    uint32_t max_reclaim_rounds;  /* default_priority - minimum_priority + 1 */
};
```

Tests may use smaller values for compact scenarios.

## 12. Anonymous/File Scan Split

Version 1 uses a simplified swappiness model.

Configuration is global by default, with optional per-domain overrides. The initial simulator defaults are `default_swappiness = 60` and `default_swap_enabled = true`; callers and traces may override them.

When swap is disabled:

```text
anonymous scan budget = 0
file scan budget = total scan budget
```

When swap is enabled:

```c
anon_weight = swappiness;
file_weight = 200 - swappiness;
```

Unused budget from a list with insufficient candidates is reassigned to the other list type.

The first version does not model dynamic reclaim cost, refault feedback, dirty/writeback ratios, or swap I/O pressure. These are future policy inputs.

## 13. Candidate Isolation and Folio Overshoot

Reclaim is two-phase:

1. Select and isolate candidates from the Shadow inactive lists.
2. Pass an isolated batch to the executor.

Isolation changes:

```text
ON_LRU -> ISOLATED
```

The candidate records the source LRU so it can be safely put back if batch construction or execution fails.

A folio is indivisible. If the next folio contains more base pages than the remaining target, the entire folio is selected. The result records overshoot:

```text
nr_overshoot_pages = nr_reclaimed_pages - target_pages
```

when the actual reclaimed amount exceeds the request.

## 14. Executor Model

```c
struct reclaim_executor_ops {
    int (*execute_batch)(
        void *context,
        struct reclaim_candidate_batch *batch,
        struct reclaim_exec_result *result);
};
```

### 14.1 User-space simulator executor

The simulator does not emulate Linux swap, writeback, unmapping, or freeing. Every folio defaults to one-shot `SUCCESS`. Tests may inject one of:

- `SUCCESS`
- `PUTBACK`
- `ACTIVATE`
- `BUSY`
- `DIRTY`
- `WRITEBACK`
- `UNEVICTABLE`

An injected result is consumed once and then resets to `SUCCESS`.

Outcome handling:

| Outcome | State update |
|---|---|
| `SUCCESS` | remove from hash table and free metadata |
| `PUTBACK` | place on original inactive list tail |
| `BUSY` | inactive putback and increment busy count |
| `DIRTY` | inactive putback and increment dirty count |
| `WRITEBACK` | inactive putback and increment writeback count |
| `ACTIVATE` | place on corresponding active list tail |
| `UNEVICTABLE` | set lifecycle state to unevictable |

### 14.2 Kernel executor

A later Linux or OpenHarmony adapter will replace the simulator executor. It will connect selected policy information to existing kernel isolation and reclaim mechanisms. It will not reimplement reverse mapping, swap, writeback, mapping removal, accounting, or physical page freeing.

The generic result is batch-oriented rather than requiring a precise error code for each real folio.

## 15. Result and Stop Semantics

Program errors and reclaim outcomes are separate.

Representative result:

```c
struct reclaim_result {
    enum reclaim_stop_reason stop_reason;

    uint64_t target_pages;
    uint64_t nr_folios_scanned;
    uint64_t nr_pages_scanned;
    uint64_t nr_folios_isolated;
    uint64_t nr_pages_isolated;
    uint64_t nr_folios_reclaimed;
    uint64_t nr_pages_reclaimed;
    uint64_t nr_pages_putback;
    uint64_t nr_pages_activated;
    uint64_t nr_overshoot_pages;

    uint32_t final_priority;
};
```

Stop reasons:

- target reached,
- no scannable pages,
- no progress,
- priority exhausted,
- executor error,
- round limit reached.

A request that reclaims fewer pages than requested can still return `RECLAIM_OK` when the engine operated correctly.

Each priority gets one complete domain traversal. Reclaim stops immediately on target completion. It also stops when the minimum priority has completed without useful progress, when no candidates exist, or when the configured round bound is reached.

## 16. Error Handling and Recovery

Representative errors:

```c
enum reclaim_error {
    RECLAIM_OK = 0,
    RECLAIM_ERR_INVALID_ARGUMENT,
    RECLAIM_ERR_NO_MEMORY,
    RECLAIM_ERR_DOMAIN_NOT_FOUND,
    RECLAIM_ERR_DOMAIN_ALREADY_EXISTS,
    RECLAIM_ERR_DOMAIN_NOT_EMPTY,
    RECLAIM_ERR_PAGE_NOT_FOUND,
    RECLAIM_ERR_PAGE_ALREADY_EXISTS,
    RECLAIM_ERR_PAGE_STATE,
    RECLAIM_ERR_PAGE_TYPE,
    RECLAIM_ERR_PARSE,
    RECLAIM_ERR_EXECUTOR,
    RECLAIM_ERR_VALIDATION,
    RECLAIM_ERR_NOT_SUPPORTED,
    RECLAIM_ERR_INTERNAL,
};
```

Required error discipline:

- Validate before mutation.
- Allocate before committing structural changes.
- A failed event leaves prior state unchanged.
- A partially built candidate batch is rolled back.
- Executor failure puts back every unresolved isolated folio.
- Partial reclaim is a normal result, not an engine error.
- Validation failure terminates the run and reports an internal bug.
- Destroying a nonempty domain is rejected.
- Destroying an engine releases every remaining object.

Putback after an internal rollback returns the folio to the correct list tail. Exact previous list position is not restored in version 1.

## 17. Validation

```c
int reclaim_engine_validate(
    const struct reclaim_engine *engine,
    struct reclaim_validation_report *report);
```

Test-mode trace replay validates after each successful event. Performance runs may validate only at the end or disable validation explicitly.

Required invariants include:

1. A folio is linked into at most one LRU list.
2. Every `ON_LRU` folio belongs to an existing domain.
3. LRU domain matches `charge_cgroup_id`.
4. An `ISOLATED` folio is not on any LRU.
5. A reclaimed folio is absent from indexes.
6. Anonymous folios only occupy anonymous lists.
7. File folios only occupy file lists.
8. Active/inactive metadata matches list membership.
9. Recorded folio and base-page totals match list traversal.
10. Global totals equal the sum of domain totals.
11. Every hash-indexed folio occupies exactly one legal lifecycle state.

Validation reports include event sequence, page ID, cgroup ID, violated invariant, expected value, and observed value where available.

## 18. Event Layer

Both direct C tests and text traces call the same public core APIs.

Initial events include:

```text
GROUP_CREATE <cgroup_id>
GROUP_DESTROY <cgroup_id>
PAGE_ADD <page_id> <cgroup_id> <ANON|FILE> <order>
PAGE_REMOVE <page_id>
PAGE_ACCESS <page_id> <access_cgroup_id>
PAGE_RECHARGE <page_id> <new_cgroup_id>
PAGE_MIGRATE <old_page_id> <new_page_id>
GROUP_SET_SWAPPINESS <cgroup_id> <INHERIT|0..200>
GROUP_SET_SWAP <cgroup_id> <INHERIT|ON|OFF>
AGE_GROUP <cgroup_id>
AGE_ALL
RECLAIM_GROUP <cgroup_id> <target_pages>
RECLAIM_ALL <target_pages>
PAGE_EXEC_OUTCOME <page_id> <outcome>
```

`PAGE_RECHARGE` is legal only for a non-isolated folio and preserves page type, age metadata, and current active/inactive class while moving ownership and list membership to the new domain. `PAGE_MIGRATE` changes the simulated page identity while preserving charge ownership, lifecycle state, type, and LRU class; the destination ID must not already exist. `PAGE_REMOVE` is rejected for isolated folios.

Assertion events may be included for scenario tests, such as page absence, lifecycle state, LRU membership, ownership, configuration inheritance, and counter values.

A parse error reports filename, line number, original text, and reason. The invalid event is not executed, replay stops by default, and final validation still runs.

## 19. Determinism

Version 1 output must not depend on wall-clock time, thread scheduling, pointer values, random hash seeds, or hash-bucket traversal order.

Deterministic rules:

- `event_seq` is the logical clock.
- Domains are processed through the sorted domain list in ascending `cgroup_id`.
- Stable diagnostic dumps sort folios by `page_id`; they never expose hash-bucket or pointer order.
- LRU head and tail semantics are fixed.
- Any rounding remainder uses stable ID order.
- Trace output excludes unstable pointer values.

The same trace and configuration must produce byte-identical output across repeated runs on the same supported platform.

## 20. Testing

CTest test groups:

- unit tests,
- integration tests,
- trace scenarios,
- allocation-failure tests,
- executor-outcome tests,
- validator corruption tests,
- long deterministic event sequences.

Required scenarios include:

- basic lifecycle,
- all four LRU transitions,
- cross-cgroup access without ownership transfer,
- directed reclaim isolation,
- global priority escalation,
- `swap_enabled = false`,
- `swappiness = 0`,
- `swappiness = 200`,
- folio order accounting,
- overshoot,
- every simulated execution outcome,
- no-progress termination,
- allocation failure with intact prior state,
- parser errors,
- engine destruction with remaining objects.

Debug configuration enables:

- `-Wall`
- `-Wextra`
- `-Wpedantic`
- `-Werror`
- AddressSanitizer
- UndefinedBehaviorSanitizer

ThreadSanitizer is deferred until the concurrent phase.

Acceptance criteria:

- all CTest tests pass,
- zero compiler warnings,
- no ASan leak, bounds, or use-after-free findings,
- no UBSan finding,
- validation passes after every successful test event,
- repeated traces produce identical output,
- no unresolved folio remains isolated after an error,
- tracked allocations return to zero after engine destruction.

## 21. Kernel Integration Direction

### 21.1 Linux validation phase

The first kernel integration is intentionally conservative:

```text
kernel global-LRU scan order
        -> query Shadow policy metadata
        -> allow, defer, protect, or lower priority
        -> use existing kernel isolation and reclaim
        -> report real scan/reclaim feedback
        -> synchronize or repair Shadow metadata
```

The Shadow layer does not directly manipulate raw kernel LRU list nodes or replace vmscan.

### 21.2 OpenHarmony phase

The OpenHarmony adapter must first establish:

- actual kernel version,
- page or folio structures,
- global LRU implementation,
- cgroup v1 memory accounting behavior,
- existing reclaim call graph,
- accessible lifecycle hook points,
- safe isolation and putback interfaces.

Only after these facts are known will the adapter map platform operations, page identity, synchronization hooks, and executor feedback to the target kernel.

## 22. Prediction Extension Direction

Prediction is not part of version 1. Later prediction inputs may include:

- next-application probability,
- foreground/background state,
- CONTINUE and REENTRY hints,
- workload state,
- TTL,
- confidence,
- Markov transitions.

Prediction may modify only policy quantities such as:

- cgroup protection weight,
- cgroup reclaim weight,
- scan budget,
- candidate filtering,
- candidate priority.

Prediction does not replace kernel page-state validation or bottom-level reclaim mechanisms.

## 23. Final Design Summary

The first implementation is a platform-independent, deterministic simulator that proves the correctness of a Shadow per-cgroup LRU policy engine. It provides cgroup-level organization, aging, pressure escalation, anonymous/file selection, folio-aware accounting, two-phase reclaim, rollback, explicit test outcomes, and rigorous validation.

The real global LRU remains the authoritative state and execution structure in future kernel integrations. Shadow metadata provides policy advice only, and real reclaim success is determined by the existing kernel memory-management path.
