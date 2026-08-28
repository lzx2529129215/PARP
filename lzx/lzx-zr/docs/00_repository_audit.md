# PARP Workload-Aware repository audit

## Scope and rule

This audit is read-only. The prototype in `lzx/lzx-zr` does not modify existing kernel, service, tool, test, configuration, or output files.

## Existing implementation

### App prediction

The existing application prediction path is under `lzx/tool/operation_predictor`:

- v1 provides application and in-application Markov baselines.
- v2 provides multi-horizon App-LSTM inference (`v2/infer/infer_app_lstm.py` and `v2/models/app_lstm.py`).
- v3 adds duration/switch-oriented application models.
- The App-LSTM branch predicts applications, not memory pages or memory-access regions.

The v4.1 reference evaluator under `lzx/kernel/v4.1-parp/tools` consumes normalized prediction rows and computes an App-level Native versus LSTM counterfactual. It does not touch kernel state.

### Runtime observation

`lzx/service/runtime_monitor` already provides:

- procfs process and cgroup observation;
- cgroup-v2 memory deltas and procfs fallback;
- global `/proc/meminfo` and `/proc/vmstat` counters;
- foreground/window lifecycle events;
- optional DAMON/tracefs region aggregation through `region_monitor`;
- working-set and App-level prediction helpers;
- observe-only memory shadow and reclaim controllers.

`region_monitor` reads `/proc/<pid>/maps`, tracks PID start time, consumes DAMON aggregated events when available, and emits sparse `region_windows.jsonl`. DAMON is sampled and aggregated; it is not an exact per-page access trace. Anonymous region identity is explicitly lower confidence than file-backed identity.

### Existing workload classification

`lzx/service/runtime_monitor/core/workload_classifier.py` classifies cgroup delta rows into one scalar state:

- `LOW_ACTIVITY`;
- `ANON_FAULT_HEAVY`;
- `FILE_FAULT_HEAVY`;
- `FILE_REFAULT_HEAVY`;
- `MAJOR_FAULT_HEAVY`;
- `MEMORY_GROWTH_HEAVY`;
- `MIXED_ACTIVE`.

The rule priority is major fault, file refault, file fault, anonymous fault, memory growth, mixed activity, then low activity. This is useful input context but it does not represent the requested independent dimensions `AccessOrder`, `ReuseMode`, `HotspotMode`, and `PhaseMode`, nor does it provide hysteresis or UNKNOWN/MIXED confidence handling.

Existing `workload_markov_builder.py` builds a second-order transition table over scalar workload IDs and state-change rows. It is a useful reference for the independent transition-baseline design, but the new prototype does not import or modify it.

## Kernel control and snapshot paths

### v4.1 historical PARP path

The historical v4 PARP patch contains `mm/parp/adapter/mglru_adapter.c`, snapshot scoring, TTL expiry, Q15 values, and writable `app_bind` / `app_prior` debugfs handlers. The adapter calls `parp_engine_score`, but the initial adapter behavior is observe-only and keeps native reclaim behavior.

### v4.2 current control path

The v4.2 patch series contains:

- `app_prior_batch` and snapshot replacement logic;
- `app_bind` concepts;
- scan-budget calculation and `scan_budget_adapter`;
- MGLRU/effective-tier adapters;
- a versioned `/dev/myfs` interface in `0007-parp-myfs-atomic-lstm-interface.patch`.

The `/dev/myfs` interface has V1 and V2 fixed-size structures. V2 carries application priors and bindings atomically, plus `policy_domain_id`, predicted working-set/resident bytes, working-set confidence Q15, and estimator version. The service-side implementation is `lzx/service/runtime_monitor/core/parp_myfs.py`. It fail-closes on absent devices, invalid predictions, and unsupported ABI, and supports `off`, `dry-run`, and `apply` modes. The new prototype uses only an offline/observe representation and never opens or writes that device.

The current service documentation states that the resident monitor does not prefetch, evict page cache, actively swap, or change memory scheduling. Prediction sinking is controlled by the kernel experiment switch.

### debugfs, ioctl, PAL, and RPC

Evidence in the current tree shows:

- historical PARP debugfs files such as `mode`, `app_bind`, `app_prior`, `app_prior_batch`, effective-tier controls, and scan-budget readouts;
- current atomic prediction submission through `/dev/myfs` ioctl V1/V2;
- no separately identified PAL or RPC control path in the inspected PARP/runtime code;
- no page-address prediction interface. Existing data is App/cgroup/domain-level, with optional region observation and working-set estimates.

The prototype therefore produces a local JSONL snapshot and a text audit adapter. It does not claim to be a kernel ABI implementation and does not write debugfs or ioctl state.

## OBSERVE, SHADOW, APPLY status

- `OBSERVE`: supported by existing collectors and the new prototype.
- `SHADOW`: supported as a local counterfactual policy calculation; it records suggested protection/reclaim/precaching/compression/migration hints while leaving native behavior unchanged.
- `APPLY`: not entered. Existing v4.2 documentation and patches gate effective-tier and reclaim-bin APPLY paths behind explicit experimental configuration and authorization. The new prototype defaults to `observe` and rejects `apply`.

## MGLRU and reclaim constraints

The inspected kernel material places scoring and scan-budget decisions inside reclaim/MGLRU paths. Those paths may hold lruvec, memcg, page-table, or reclaim-specific locks and may run in allocation-sensitive or reclaim recursion-sensitive contexts. A user-space predictor must not perform model inference, blocking I/O, allocation-heavy work, or arbitrary cgroup operations from those contexts.

The safe boundary for this phase is:

1. collect and aggregate observations outside the reclaim path;
2. compute a bounded, versioned snapshot in user space;
3. use TTL and sequence checks;
4. let kernel-side consumers treat invalid/expired/unknown data as Native;
5. keep any future page-level adapter bounded and separately reviewed.

No source in the allowed implementation directory can safely establish a new in-kernel page access hook or prove its lock/context behavior. This phase therefore emits an adapter contract and patch skeleton documentation rather than changing Linux source.

## Required design adjustment

The new Workload-Aware prototype will:

- consume sparse region windows and cgroup/global delta rows when available;
- mark address-order dimensions UNKNOWN when region order is absent or low resolution;
- compute only evidence-backed features;
- classify four independent dimensions plus a dominant label;
- include UNKNOWN and MIXED, confidence, hysteresis, minimum dwell, and cooldown;
- provide rule-trend, second-order Markov, and lightweight score-based temporal prediction;
- serialize Q15 confidence/probability, `prediction_seq`, `horizon_ms`, `ttl_ms`, WSS and slope;
- produce OBSERVE and SHADOW strategy hints only;
- fall back to Native when stale, invalid, low-confidence, or incompatible.

## Cannot be safely implemented in this phase

- exact per-page access order from current procfs/cgroup/DAMON aggregate inputs;
- a claim that App-LSTM predicts pages;
- direct modification of MGLRU generation/list ordering or page tier in this isolated prototype;
- real preclean, compression, migration, page eviction, or swap operations;
- a new kernel patch that can be compiled and validated without the exact kernel worktree and build environment;
- APPLY behavior or an assertion of production reclaim gains.

The acceptance target is therefore decision quality and fail-closed behavior in replay/fixture tests, followed later by authorized native/shadow runtime experiments.
