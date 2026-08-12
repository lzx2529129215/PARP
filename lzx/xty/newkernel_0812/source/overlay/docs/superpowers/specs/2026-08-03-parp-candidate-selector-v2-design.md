# PARP Phase2.10B: Causal Stratified Reclaim Candidate Selector v2

## Scope and safety

This phase is offline-only. It reads frozen Phase2.10B pilot trace and frozen
Phase2.9A decisions, writes only the new Phase2.10B output tree, and never
starts a GUI/application, collector, DAMON, cgroup, pressure workload, kernel
build/install, or Apply path. No QQ pilot labels are used to choose strata or
quotas.

## Causal candidate universe

For each decision window, the parser first closes the observation at time `t`.
It aggregates only evidence at or before `t`, keyed by `(dev, inode,
file_version, partition_generation, segment)`. A candidate is admitted only if
the file identity/version and partition generation are valid, it was observed
at `t`, it is explicitly inactive at `t`, and it has a valid age/history. Active
and unobserved regions are excluded before any selector runs. FileId is used
only for identity/version checks and deterministic tie-breaking; no path,
filename, content, operation, automation, session, or repeat field is a
feature or stratum input.

## Legacy reproduction

`GENERATION_TAIL_128_V1` is reproduced with the frozen Phase2.10 selector
ordering and candidate pool. The expected QQ pilot audit is 36 complete 60s
decisions, 4,608 selected candidates, zero selected positives, and 3,334
positive candidates outside the selection. Any material mismatch stops the
pipeline before v2 claims are made.

## Selector variants

- S0 keeps the legacy cold-tail ordering.
- S1 assigns dynamic generation strata from the observed generation ranks and
  fills `Q_BALANCED`, `Q_COLD_HEAVY`, and `Q_MIDDLE` quotas.
- S2 assigns four current-decision recency quantiles and uses the same quotas.
- S3 is the primary hybrid: H0 extreme tail, H1 deep cold, H2 boundary cold,
  H3 recent inactive. It fills strata in H0→H1→H2→H3 order and records every
  cross-stratum fill.
- S4 is only a fallback when generation is unavailable and uses age, recency,
  and consecutive inactivity.

All selectors are deterministic, select at most 128 unique candidates, never
copy candidates to fill a short universe, and use the same fixed candidate
count and hypothetical reclaim/protection budgets for ranking comparisons.

## Label isolation and horizons

Selector output is written and hashed before the independent labeler reads any
future window. The labeler emits availability and positive/negative/unknown
labels for 10s, 30s, 60s, and 120s. Future NOT_OBSERVED or incomplete horizons
remain unavailable rather than being converted to negative labels.

## Selection and evaluation

Selector choice uses only the predefined WPS/FILES validation role (`wps_02`)
and hard legality/realism gates. QQ pilot and final test sessions are never
used to tune strata, quota, or thresholds. The selected v2 is frozen in
`config/frozen_selector_v2.json` before test evaluation. Oracle comparisons are
within one selector, one decision, one candidate hash, and one reclaim budget;
cross-selector proxy differences are reported only as selector diagnostics.

## Final state semantics

`PARP_PHASE210B_CANDIDATE_SELECTOR_V2_VALIDATED` means only that this offline
candidate reconstruction has causal inactive candidates, support, realism,
and an Oracle headroom on frozen traces. It does not claim real reclaim,
refault, latency, or App-specialized model gains. If support or realism is
insufficient, the result is `...POSITIVE_SUPPORT_INSUFFICIENT` or
`...REALISM_GATED`; if only the offline pipeline completes with limitations,
`...OFFLINE_SELECTOR_PIPELINE_COMPLETE_LIMITED` is used.
