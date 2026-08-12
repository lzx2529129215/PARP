# PARP Phase2.8 Kernel-State-Guided Page Prediction Design

## Research contract

The only upper-layer online semantic input is `foreground_app_id`.  AppBind
resolves a target domain/memcg; FILE/ANON evidence and cgroup/kernel counters
provide all other current and historical inputs.  Automation operations are a
physically separate supervision stream and are loaded only after a prediction
is serialized.  Paths, names, extensions, titles, GUI/input events, content,
and application logs never enter a model vector.

Phase2.7B's negative result is an invariant: its high-dimensional concrete-file
Page-State Markov had 100% UNKNOWN under cross-session shift.  Phase2.8 does not
disable rejection.  It replaces concrete identity features with aggregate
kernel state and compares direct, semantic, and fused segment routes.

## State machine and authorization

The state machine is `AUDIT -> EXISTING_DATA_ANALYSIS -> COLLECTION_DESIGN ->
AWAITING_COLLECTION_AUTHORIZATION -> REPEATED_COLLECTION -> DATASET_BUILD ->
CURRENT_OPERATION_MODEL -> NEXT_OPERATION_MODEL -> ACCESS_PATTERN_MODEL ->
SEGMENT_PREDICTION_MODEL -> ONLINE_REPLAY -> REFAULT_SIMULATION ->
KERNEL_OBSERVE_INTEGRATION -> CONTROLLED_AB_TEST -> COMPLETE`.

State is atomically replaced in `outputs/parp_phase28_runtime_state/state.json`.
Repeated collection requires an interactive user-owned GUI session plus root
trace/DAMON/cgroup setup.  No password is stored, sudoers is not changed, and
the system stops at the authorization gate until the user explicitly runs the
prepared command.  Reboot and Apply are independent later authorizations.

## Three prediction routes

- **DIRECT**: causal page/kernel history to segment future probabilities.
- **SEMANTIC**: kernel state to current-operation probabilities, predicted next
  operation, access-pattern probabilities, then segment probabilities.
- **FUSED**: DIRECT features plus all three probability vectors.  Probabilities
  are optional evidence, never hard gates.

All routes predict cumulative P10/P30/P60 and P(cold 60s), with UNKNOWN,
confidence, TTL, generation, and version semantics.  Online output is calibrated
to P10<=P30<=P60.  Low probability is not a reclaim command.  The first eligible
action is bounded high-confidence `PROTECT`; `PREFER_RECLAIM` is out of scope.

## Existing-data pilot

The immutable five Phase2.7B sessions are `RUNTIME_PHASE27B_REAL_FRESH_REUSED`.
They provide PARP FILE/ANON access, age, size, logical page intervals,
multi-resolution segments, and aligned operation labels.  They do not contain
per-window cgroup memory/CPU/I/O/PSI, fault/refault/reclaim, task, scheduler,
dirty/writeback, RSS/PSS, or swap series.  Missing groups use explicit
availability masks and cannot add signal.

The pilot builds causal 2s/5s/10s PAGE/ANON windows, deterministic Top-K plus
OTHER vectors, coarse current-operation classifiers, weak access-pattern
labels, direct/semantic/fused segment prototypes, online replay, and an offline
fixed-budget refault proxy.  Its purpose is feasibility and collection design;
it cannot validate unseen-document repeatability or real refault reduction.

## Repeated collection

WPS collection uses at least three sessions and three dedicated fixture copies
covering small/medium/large sizes.  Each coarse operation repeats at least five
times per session, with >=20s stable baseline, >=20s action, and >=20s recovery.
Operation order rotates deterministically by session seed.  Files uses at least
two sessions and three repeats per operation on a dedicated fixture directory.
Every event has a repeat ID.  User documents and chat are never read.

At one-second cadence the collector writes a kernel stream separate from label
events: PARP FILE/ANON, cgroup v2 memory/current/events/stat/swap, CPU, I/O and
PSI, task count, selected proc counters, and root-owned trace metadata.  Counter
features are deltas/rates.  Missing interfaces are recorded as unavailable.
Cleanup always stops owned DAMON/trace/scope resources and restores observe
modes; it never touches unrelated processes.

## Representation and models

File identity is internal only for aggregation, history, and final key mapping.
Top-K 1/3/5/8 uses unique, weighted, and hybrid activity scores with stable
identity tie-breaking.  OTHER conservation is mandatory.  V1 PAGE, V2 PAGE+VM,
V3 FULL_CURRENT, and V4 FULL_TEMPORAL compare 2s/5s/10s windows and causal
history 1/2/3/6.

Small-data models are majority/transition/recent-frequency and balanced linear
models.  sklearn-only trees run only if already installed.  Current operation,
next operation, access pattern, and segment tasks have independent UNKNOWN
thresholds selected on validation.  Segment candidates exclude cross-session,
expired-version, and no-history NOT_OBSERVED objects.

## Access patterns and refault proxy

Access-pattern weak labels are derived only from page/kernel geometry:
sequential direction, local loop, random jump, expansion/contraction, long
reuse, burst write, idle cooling, mixed, unknown.  Operation labels never define
patterns.

Offline replay compares native approximation, current hotness, last/recent,
direct, operation-assisted, pattern-assisted, fused, and oracle at equal protect
budgets.  A protected segment is a hit if accessed in the future horizon;
waste, false-cold, normalized future-refault proxy, and oracle gap are reported.
This simulation is not a claim about measured `workingset_refault_*`.

## Kernel and Apply gates

No kernel source is changed unless all twelve offline observe gates pass.
Otherwise the result is `PARP_PHASE28_MODEL_IMPROVEMENT_REQUIRED` or an earlier
collection/pilot state.  If eligible, an RCU snapshot design permits only
allocation-free read lookup in MGLRU and records suggestions without changing
native decisions.

Apply remains disabled until a separate explicit user authorization in the
current session and all safety gates pass.  Any later experiment is isolated to
one test memcg, Protect-only, budgeted, TTL-limited, watched, and reversible.
