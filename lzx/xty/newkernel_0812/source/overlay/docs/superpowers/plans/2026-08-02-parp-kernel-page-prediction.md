# PARP Phase2.8 Implementation Plan

1. Verify baseline HEAD, branch, kernel, Phase2.7B completion, interfaces, and
   immutable inputs; initialize the atomic Phase2.8 state machine.
2. Freeze all reused raw hashes and inventory actual PAGE/ANON versus missing
   VM/I/O/CPU/PSI/refault sources.
3. Use TDD to implement deny-list/source-map contracts, causal windows,
   deterministic Top-K/OTHER conservation, UNKNOWN/TTL/version safety, segment
   horizon labels, normalized refault proxy, and Apply budget/domain guards.
4. Run an existing-data 2s/5s/10s kernel-only operation separability pilot;
   preserve Phase2.7B's UNKNOWN result and determine whether PAGE/ANON signal is
   sufficient.
5. Implement a repeated WPS/Files runner, kernel/cgroup sampler, separate label
   writer, manifest/checkpoint, randomized operation schedules, fixture guards,
   and idempotent cleanup.  Do not execute it without interactive authorization.
6. Stop at `AWAITING_COLLECTION_AUTHORIZATION` with one exact manual command if
   root/GUI collection is required.  Do not store credentials or modify sudoers.
7. After authorization, validate repetition counts, trace loss, apply delta,
   cleanup, raw hashes, and session/document splits before model building.
8. Build V1-V4 vectors, current/next operation models, rule-audited access
   patterns, and A/B/C/D segment samples with 10/30/60 availability.
9. Select all dimensions, thresholds, and hyperparameters on train/validation;
   evaluate the held-out session/document test exactly once.
10. Run causal online replay and fixed-budget refault strategy simulation.
11. Implement Observe-only snapshot support only if every offline gate passes;
    otherwise record the precise failed gates and leave kernel source unchanged.
12. Never execute Apply without a new explicit authorization.  If later
    authorized, restrict it to bounded high-confidence protection in one test
    domain with watchdog and rollback.
13. Re-hash inputs, run standard-library tests/static/schema checks, create local
    commits, atomically advance state, and generate the Chinese final report.

## Self-review

- Existing-data pilot and fresh repeated data retain distinct source markers.
- Label and kernel streams are physically separate; deny-list validation runs
  at schema construction and online serialization.
- File identity never enters numeric/categorical model columns.
- No future window is available to feature builders or online state.
- Missing cgroup counters have availability=false, not observed zero.
- Direct, semantic, and fused results are all kept; fused success is conditional
  on measured improvement.
- Refault replay normalizes by comparable protect/reclaim budget and is labeled
  simulation, never kernel evidence.
- UNKNOWN and false-cold limits are not relaxed to produce nonzero suggestions.
- Observe and Apply gates are independent; no reboot is implicit.
- No push/reset/clean and no mutation of frozen worktrees/raw.
