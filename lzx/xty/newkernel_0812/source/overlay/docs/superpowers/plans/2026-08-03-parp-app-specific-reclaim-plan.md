# PARP Phase2.10 Implementation Plan

## Stage 1 — safe ordinary-user work

1. Locate the project from required directory markers and verify the baseline.
2. Freeze cryptographic manifests for Phase2.8 raw input, Phase2.8B output and
   Phase2.9A output without modifying any of them.
3. Inventory whole sessions by app and reserve all future mixed-scenario runs
   exclusively for test/A-B.
4. Audit existing automation, cgroup v2 controllers and PARP interfaces
   read-only.
5. Implement the 40 deterministic contracts using TDD.
6. Produce the mixed-scenario, fixture, cgroup, pressure, Observe, randomized
   run and cleanup designs as non-executed artifacts.
7. If QQ lacks independent train and validation sessions, write the exact gate,
   authorization request and final report and stop.

## Stage 2 — QQ collection, only after explicit authorization

Use a test QQ account or privacy-safe offline mock and generated fixture
content.  Collect at least two independent QQ candidate-level sessions, one for
training and one for validation.  Do not reuse the future A/B scenario.  Verify
target cgroup membership, trace loss, schema, privacy, cleanup and input hashes.

## Stage 3 — offline gate

Fit Generic, WPS, FILES and QQ pairwise reuse rankers using one shared causal
schema.  Select all thresholds from train/validation only.  Evaluate the full
3×4 app/model matrix on held-out sessions with identical candidate/reclaim
hashes.  Stop if G0 or G1 fails.

Execution result (2026-08-03): all four models and the matrix were produced,
but the QQ evaluation split had zero positive future-reuse candidates in the
frozen generation-tail pool.  WPS/FILES also had fewer than ten
pairwise-evaluable evaluation decisions.  Stage 3 therefore stops at
`PARP_PHASE210_QQ_MODEL_DATA_INSUFFICIENT`; Stage 4 is not authorized or
scientifically reachable from this dataset.

## Stage 4 — authorized Observe and pressure calibration

Run fixture-only P0 mixed automation, then measure working-set statistics and
derive P1–P3 limits.  Request explicit values and commands before changing the
test parent cgroup.  Run Observe-only at P0–P3; actual reclaim remains Native.
Stop on routing, ranking, latency, trace-loss or safety failure.

## Stage 5 — separately authorized Apply A/B

Only after a safe bounded scorer exists, run the frozen Latin-square schedule
for five strategies and four pressures.  Pair by scenario seed, pressure and
block.  Watch OOM, PSI, P99 latency, throughput, swap, trace loss and target
escape.  On any stop condition disable Apply, restore Native and cgroup values,
stop pressure/apps, remove scopes/traces and preserve logs.

## Verification and reporting

Run 40 unit contracts, `py_compile`, JSON/JSONL parsing and `bash -n` for any
generated shell.  Recompute frozen manifests.  Tables A–E must explicitly show
`NOT_RUN` for gated real metrics.  Only G0–G8 success permits the validated
status; offline proxy evidence is labeled as such.
