# PARP effective-tier live-shadow audit <!-- #lzx -->

This branch begins at `aa39d99696392c0bfaba5df2e6152e24a9f88a6d` and is
limited to Phase-F observability and offline counterfactuals.  It does not
authorize, compile-enable, or execute an APPLY mode.

## Native-behaviour invariant

The MGLRU call site remains `mm/vmscan.c:sort_folio()`.  In
`PARP_EFFECTIVE_TIER_SHADOW`, `parp_effective_tier_actual_protect()` selects
the native protection decision; the scored effective tier is trace-only.
`CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY=n` rejects all four APPLY
setters.  The graded fixed/binary/pressure-aware values are stored in
`struct parp_tier_decision` but are never used by the MGLRU list movement or
isolation path.

## Pressure state

`LOW`, `MEDIUM`, `HIGH`, and `CRITICAL` are derived only from the
already-held reclaim-local snapshots: reclaim priority, reclaim target and
progress, and the per-lruvec no-progress counter.  No PSI, cgroup file,
allocation, sleeping, I/O, or mutex operation occurs under `lru_lock`.
PSI/cgroup samples are reserved for the userspace calibration join.

The engineering matrix is version 1 and is deliberately tagged
`ENGINEERING_PRESSURE_POLICY_UNVALIDATED`:

| Level | Upgrade Q8 | Downgrade Q8 |
| --- | ---: | ---: |
| LOW | 256 | 128 |
| MEDIUM | 192 | 256 |
| HIGH | 64 | 256 |
| CRITICAL | 0 | 0 |

All deltas are SHADOW counterfactuals.  Cold downgrades are constrained to an
ordinary native boundary and special Native protection is retained.  The
offline replay checks these invariants, marks its labels
`FUTURE_REAL_ACCESS_NOT_REFAULT`, and leaves unavailable scan/reclaim effects
as null.

## Trace and batch accounting

Decision traces include reclaim context, local-pressure fields, fixed/binary/
graded deltas, policy provenance, and both fixed and graded effective-protect
states. `tier_gate_decisions` in the effective-tier stats is an explicit alias
of the all-candidate atomic counter used for coverage reconciliation.

The initial batch trace is emitted before reclaim while the scan context is
valid.  A second `reclaim_result=1` trace after `shrink_folio_list()` carries
the actual reclaimed-page count.  `collector.py` joins it solely by exported
experiment/session/batch identity and records
`batch_reclaim_result_observed`; it does not infer reclaim from access events.

## Real-access audit

`__parp_effective_tier_note_access()` changes last-access and reuse state only
when `parp_access_event_is_real()` accepts the event.  Move/promotion paths use
`__parp_effective_tier_note_move()` and emit `real_access=false`; they update
generation bookkeeping but not last-access state.  The KUnit lifetime test
covers policy/native move non-contamination, and collector rejects policy
moves as future-access labels.  Live use remains gated on a post-boot smoke
trace containing file, anon, candidate, trusted-access, outcome, batch, and
lock events with cookie/lifetime/session linkage.

### Candidate-correlated future-access trace <!-- #lzx -->

The access trace is now deliberately bounded: it is emitted only for a
trusted real-access event that observes a nonzero page-local reclaim-candidate
count before that access clears the count.  The exported
`candidate_count` must be positive; the offline collector fails closed on a
missing or zero value.  Policy moves never produce this event.  This preserves
the candidate-time to next-real-access join required by the ranking target and
prevents global page-access trace flooding.

## Live boundary

`tools/parp/effective_tier/live_shadow_preflight-lzx.py` is read-only. It records
kernel, boot, debugfs, sudo, and test-scope facts into an output skeleton but
cannot install a kernel, toggle mode, write a cgroup, launch pressure, or
reboot. The root-only collection phase must stop if its preflight detects an
unavailable target kernel, cgroup scope, or recoverable boot path.
