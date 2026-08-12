# PARP MGLRU effective-tier implementation

## Status and scope

This document describes the effective-tier kernel contract in the
`feat/parp-effective-tier` worktree and its Phase-E offline boundary.  The
implementation is deliberately runtime-default-OFF.  It has not collected an
authorized real SHADOW dataset, installed or booted a kernel, changed a
cgroup, generated memory pressure, or executed an APPLY mode.

The embedded six-feature lookup table and its thresholds are deterministic
engineering fixtures for implementation and Python/C parity tests.  They are
not a trained Bradley–Terry model, calibrated probabilities, or evidence that
Protect-only or Bidirectional APPLY is safe.  The next allowed operational
state is therefore:

`PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED`

The source-level MGLRU findings that define the integration point are recorded
in `docs/parp_effective_tier_source_audit.md`.  The offline model contract is
recorded in `docs/parp_effective_tier_pairwise_ranking.md`.

## Design outcome

PARP does not replace MGLRU's native tier.  It computes a virtual tier for the
one ordinary tier-protection decision that MGLRU is about to make:

```text
offline training:  bounded Bradley–Terry next-real-access pairs
kernel inference:  one independent additive integer score per folio
kernel decision:   score thresholds -> Q8 delta -> effective tier
```

The distinction is fundamental.  The training loss compares pages, but the
kernel never collects a candidate set for ranking.  For each folio it performs
a fixed number of feature-bin lookups, integer additions, four ordered
threshold comparisons, and fixed-point arithmetic.  There is no runtime sigmoid,
probability, pairwise comparison, sort, heap, tree, dynamic allocation,
floating point, I/O, RPC, or unbounded loop.

The runtime complexity is `O(fixed features + fixed thresholds)` per folio,
not `O(K log K)` or `O(K²)` over a candidate batch.

The offline bounded pair sampler defaults strictly to 64 pairs per group and
supports the declared cap ablations `[32, 64, 128]`; its manifest records both
the selected cap and the supported set.

## Exact MGLRU integration boundary

The integration is inside `mm/vmscan.c:sort_folio()`, after the unevictable and
already-promoted/race checks and at the ordinary native tier gate.  The native
values are derived from the same coherent folio-flag snapshot:

```text
refs          = folio_lru_refs(folio)
workingset    = folio_test_workingset(folio)
native_tier   = lru_tier_from_refs(refs, workingset)
native_protect = native_tier > native_tier_idx
```

`native_tier_idx` remains MGLRU's per-lruvec, per-anon/file dynamic boundary,
derived from the native refault/evicted/protected controller.  PARP neither
replaces that controller nor writes its arrays.

Only the ordinary strict `native_tier > native_tier_idx` protection can be
experimentally reclassified.  The following remain Native and cannot be
cancelled by the first implementation:

- unevictable handling;
- a folio already moved out of the current oldest generation;
- the saturated lazy/workingset condition
  `refs + workingset == BIT(LRU_REFS_WIDTH) + 1`;
- strong ordinary protection at `native_tier >= native_tier_idx + 2`;
- a zone above `reclaim_idx`;
- writeback and file-dirty handling;
- all subsequent GFP/swap, reference, LRU-race, isolation, putback, and reclaim
  conditions.

A predictive upgrade changes an ordinary native-reclaim decision into one
policy protection and moves the folio at most one generation.  It does not
set an access bit, change native refs/tier, or increment native protected
statistics.  A predictive downgrade is permitted only at
`native_tier == native_tier_idx + 1`; it skips this round's ordinary tier
promotion, does not write a lower native tier or older generation, and then
continues through the remaining native `sort_folio()` and isolation checks.

Consequently, a trace outcome such as `PREDICTIVE_DOWNGRADE` means that the
ordinary tier protection was bypassed.  It does not mean the folio was
necessarily isolated, reclaimed, or refaulted.

## GLOBAL integer scorer

There is exactly one `GLOBAL_REUSE_MODEL`.  There is no WPS, FILES, QQ, App,
process, path, session, workload, or frontier-based runtime routing.  The
schema-v1 score is:

```text
reuse_score = quantized_bias
            + sum(quantized_weights[feature][bin(feature_value)])
```

The six fixed runtime features, all captured at the candidate decision time,
are:

| Index | Feature | Runtime representation |
|---:|---|---|
| 0 | `time_since_last_real_access_ms` | non-negative integer |
| 1 | `previous_real_access_interval_ms` | non-negative integer |
| 2 | `reuse_interval_ema_ms` | non-negative integer |
| 3 | `consecutive_reclaim_candidate_count` | saturating count |
| 4 | `time_in_current_generation_ms` | non-negative integer |
| 5 | `access_ema_q8` | unsigned Q8 history |

Each feature has five fixed edges and six bins.  An edge belongs to its lower
bin.  The accumulator is checked before conversion to `s32`; missing state,
an `S64_MIN` feature sentinel, model/schema mismatch, or overflow invalidates
the result.

The primary ranker intentionally excludes `native_tier` and
`native_tier_idx`, because native frequency evidence already participates in
the effective-tier formula.  Including native tier, and including both native
tier and tier index, remain offline ablations rather than v1 kernel inputs.

`reuse_score` is an ordinal utility: a higher score means an earlier expected
next trusted real-access observation and higher short-term reuse value.  It is
not a reuse probability, calibrated probability, or refault probability.

## Q8 score-to-tier contract

The fixed-point scale is:

```c
#define PARP_TIER_SCALE 256
#define PARP_MAX_TIER   3
```

The canonical main-path field names are:

- `reuse_score`;
- `score_threshold_cold`;
- `score_threshold_hot_1`;
- `score_threshold_hot_2`;
- `predictive_delta_tier_q8`;
- `effective_tier_q8`.

The model artifact also carries nullable `score_threshold_hot_3`, but only as
the boundary of the separate experimental +3 contract.  Its default is
`null`; it is not a fourth primary-path threshold.

The normalized offline record currently retains `cold_threshold`,
`hot_threshold_1`, `hot_threshold_2`, `hot_threshold_3`, and `delta_tier_q8`
only as deprecated, equal-valued compatibility aliases.  They must never
acquire probability semantics.

The primary model artifact has exactly this nominal Q8 mapping:

```text
cold    -> -256
neutral ->    0
hot_1   -> +256
hot_2   -> +512
```

Accordingly, both its `delta_tier_q8` and `predictive_delta_tier_q8` objects
contain only `cold`, `neutral`, `hot_1`, and `hot_2`.  By default,
`score_threshold_hot_3 = null` and `experimental_plus3` records
`status = NOT_SELECTED`, `default_enabled = false`, and
`apply_supported = false`.

The kernel engineering fixture retains the explicit score thresholds -48,
48, 96, and 144 and defaults `max_upgrade_tiers` to 2.  Its fixed-cost
score-to-policy mapping is:

```text
reuse_score <= score_threshold_cold
    -> predictive_delta_tier_q8 = -256

score_threshold_cold < reuse_score < score_threshold_hot_1
    -> predictive_delta_tier_q8 = 0

score_threshold_hot_1 <= reuse_score < score_threshold_hot_2
    -> predictive_delta_tier_q8 = +256

reuse_score >= score_threshold_hot_2
  and reuse_score < score_threshold_hot_3
    -> predictive_delta_tier_q8 = min(max_upgrade_tiers, 2) * 256

reuse_score >= score_threshold_hot_3
    -> predictive_delta_tier_q8 =
       (max_upgrade_tiers == 3 ? +768
                               : min(max_upgrade_tiers, 2) * 256)
```

`max_upgrade_tiers` supports +1, +2, and experimental +3 model replay.  A +3
threshold may enter an artifact only after explicit validation selection,
recorded as `hot_threshold_3_selected_on_split = validation` on input and
`experimental_plus3.threshold_selected_on_split = validation` on export.
Even then, +3 is offline/SHADOW-only, `default_enabled` remains false, and
`apply_supported` remains false.  The mode setter rejects every APPLY mode
while the configured cap exceeds +2; the kernel has no production +3 APPLY
configuration.  The separate boundary preserves a +2 bucket between `hot_2`
and `hot_3` during an authorized SHADOW +3 ablation.  With cap at most 2,
scores at or above `hot_3` retain the configured +1/+2 cap.  The cold side is
always capped at -1.  The fixture value 144 is engineering scaffolding, not a
validation-selected +3 threshold.

The effective decision is:

```text
native_tier_q8 = native_tier * 256

effective_tier_q8 = clamp(native_tier_q8 + predictive_delta_tier_q8,
                          0, PARP_MAX_TIER * 256)

effective_protect = effective_tier_q8 > native_tier_idx * 256
```

The comparison remains strictly greater.  Equality does not protect.

On an invalid model, invalid/missing/unstable metadata, model-version or
configuration race, exhausted action budget, repeated per-folio epoch claim,
or commit-time folio-state race, the affected action's applied delta becomes
zero and it falls back to Native.  Both predictive upgrades and downgrades
additionally fall back under severe pressure or no progress:

```text
predictive_delta_tier_q8 = 0
effective_protect == native_protect
```

This is the central fail-safe invariant.

## Runtime modes and authorization

`CONFIG_PARP_EFFECTIVE_TIER` is `default n`, selects `PAGE_EXTENSION`, and is
mutually exclusive with the older frontier-score experiment.
`CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY` is separately `default n`.
When the latter is disabled, all APPLY setters return `-EOPNOTSUPP`.

| Mode | Score/trace | Reclaim behavior |
|---|---|---|
| `OFF` | effective scorer fast path disabled | exactly Native |
| `SHADOW_EFFECTIVE_TIER` | computes counterfactual quadrants | exactly Native |
| `APPLY_PROTECT_ONLY` | permits bounded upgrades | never applies downgrades |
| `APPLY_BIDIRECTIONAL` | permits bounded upgrades and boundary-only downgrades | authorization required |
| `APPLY_RANDOM_MATCHED` | hashed random baseline using configured rates | authorization required; limitation below |
| `APPLY_RECENCY_BASELINE` | recency-only score baseline | authorization required |

Configuration writes are accepted only while OFF.  Active-to-active mode
switches return `-EBUSY`, requiring an OFF transition.  An OFF-to-active
transition advances the state epoch and clears a prior state-fault latch.  A
concurrent MGLRU state change faults the experiment until another explicit
OFF-to-active transition.

Upgrade and downgrade accounting are independent.  Each has batch pages,
epoch pages, and a base-page ratio limit.  A folio may claim each action at
most once per epoch.  A shared severe-pressure/no-progress gate disables both
directions; downgrades additionally require the boundary tier, may require two
cold observations, and never override special or strong Native protection.
State and reservation revalidation prevents a decision from being committed
after its feature/tier snapshot has changed.

## Page state and trusted access boundary

The effective-tier `page_ext` payload is exactly 24 bytes per base page:

```text
last_access_ms, previous_interval_ms, reuse_interval_ema_ms,
generation_enter_ms, lifetime_epoch, packed state, decision epochs
```

Score and native tier are not persisted.  A keyed folio cookie plus lifetime
epoch prevents a recycled PFN from joining an older folio lifetime.

Only audited real-access event classes can update `last_access`, interval/EMA
history, access count state, or consecutive-candidate state:

- a successfully observed/cleared PTE or PMD young bit;
- an explicitly audited `MARK_ACCESSED` source;
- a successful buffered/splice `FD_REFERENCE` source.

The generic `folio_mark_accessed()` source is deliberately untrusted unless a
caller supplies an audited source.  Policy marks invalidate confidence rather
than fabricate access history.  Native tier promotion, native generation
maintenance, PARP policy promotion, putback, and list movement update only
movement/generation bookkeeping; they do not update the last-real-access
timestamp.  This prevents a PARP promotion from becoming self-reinforcing
training evidence.

These hooks provide conservative observed access evidence, not a complete log
of CPU accesses.  A young bit reports that an access occurred since the last
clear and is timestamped when observed, not at the exact instruction.  Access
may be coalesced or missed between observations.  This false-negative and
timing-interval boundary must be carried into ranking-quality uncertainty.

### `page_ext` memory cost

Assuming 4 KiB base pages and excluding allocator/alignment overhead outside
the 24-byte PARP payload:

| Physical memory | PARP payload | Fraction of physical memory |
|---:|---:|---:|
| 16 GiB | 96 MiB | 0.5859375% |
| 32 GiB | 192 MiB | 0.5859375% |
| 64 GiB | 384 MiB | 0.5859375% |

## Trace and latency observability

The effective-tier trace family records decision, access, outcome, batch, and
lock events.  A single monotonic `trace_sequence` spans all five kinds so the
offline collector can detect internal gaps and duplicates.  Access, outcome,
and batch events carry explicit experiment/session ownership; candidate
identity includes the folio cookie and lifetime epoch.  Invalid feature state
is exported as `features_valid=false` and normalized to six `null` feature
values, never six fabricated zeros.

Trace construction, cookie hashing, and timestamps are behind each
tracepoint's enabled check.  The mandatory state and counter work remains in
the active effective-tier path.

Lock tracing separates:

- wait time, from the lock-attempt timestamp to acquisition;
- held time, from acquisition to immediately before unlock;
- the IRQ-disabled interval where the architecture/configuration permits it.

On non-RT builds the IRQ-disabled sample begins just after IRQ disable and ends
just before IRQ enable, so it is a lower-bound measurement by a few
instructions.  On `CONFIG_PREEMPT_RT`, the native `spin_lock_irq()` primitive
is preserved; wait and held time remain measured, but IRQ-disabled duration is
unmeasured and must be normalized to `null`, not a fabricated zero.

The instrumented lru-lock scopes are the scan/isolation lock section and the
later batch putback/accounting lock section in `evict_folios()`.  They are not
a measurement of every `lruvec->lru_lock` user or of an entire direct-reclaim
request.  Separate exported reclaim telemetry is required for those claims.
Likewise, sequence-gap detection only covers records that reached the trace
stream; independently measured trace-buffer loss and tier-gate counter
coverage remain authoritative at capture boundaries.

The kernel decision trace records mode, integer score, rank-score bin, all four
thresholds, features, model validity, expected/actual model version, feature
schema, scorer identity/provenance, checksum, and timings.  The collector
normalizes it into raw-event schema v2; labeled-candidate and observability
schemas are also v2, while session metadata remains v1 and the runtime feature
schema remains v1.  GLOBAL decisions identify `pairwise_linear_ranker` and the
checksum of the embedded scorer parameters with provenance
`ENGINEERING_FIXTURE_UNTRAINED`; the checksum is not training data or
model-quality evidence.  RECENCY and RANDOM_MATCHED use distinct scorer
identities, `CONTROL_BASELINE` provenance, and no pairwise model checksum.  The
collector checks mode/scorer agreement.  A live model comparison must still
bind every export to the corresponding immutable model manifest.  Score
percentile is an offline derivative, not a kernel hot-path computation.

## Four counterfactual quadrants

SHADOW records the native and effective decisions but applies Native behavior:

| Native decision | Effective decision | Action |
|---|---|---|
| reclaim | reclaim | `KEEP_RECLAIM` |
| reclaim | protect | `PREDICTIVE_UPGRADE` |
| protect | protect | `KEEP_PROTECT` |
| protect | reclaim | `PREDICTIVE_DOWNGRADE` |

Special Native protection is reported separately and forced protected.  Counts
and rates use `folio_nr_pages` so a large folio is represented in base pages.
Future access is not called a refault.  A real refault claim requires native
`workingset_refault_file`/`workingset_refault_anon` counters or another
verified workingset event.

## Validation boundary and known limitations

The current validation record supports the focused integer scorer/reference
and effective-tier KUnit work, but it does not authorize live use.  A filtered
effective-tier UML run reported all 16 focused effective-tier cases passing.
The full legacy `parp` suite did not pass: pre-existing, non-effective-tier
failures include `parp_evidence_window_test` returning `-ESTALE` and a SIGFPE
near `parp_compute_scan_budget`.  The full legacy suite must therefore not be
reported as PASS.  The enabled `bzImage` and modules, feature-disabled
`bzImage`, focused non-RT/PREEMPT_RT objects, and the Python regression matrix
were instead validated independently on the final integration tree.

The remaining limitations and gates are explicit:

1. **No real ranking evidence.** No authorized exported SHADOW dataset,
   trained ranking model, real session/pair counts, validation thresholds,
   pairwise accuracy, NDCG, C-index, score monotonicity, four-quadrant rates,
   reclaim benefit, refault delta, App latency, or lock/direct-reclaim tails
   exist.  Those values are NOT COLLECTED or NOT EVALUATED, never inferred
   from fixtures.
2. **Random baseline is rate-matched, not count-matched.** The online
   `APPLY_RANDOM_MATCHED` path uses a deterministic hash and configured upgrade
   and downgrade rates.  It does not guarantee exactly the same realized
   counts as the model in each stratum.  An exact reservoir helper test does
   not make the online baseline exact.
3. **Rank feedback is not implemented.** No separate
   `rank_feedback[anon/file][native_tier][score_bin/action]` arrays exist yet.
   Native refault PID statistics are intentionally untouched.  No online
   weight or threshold adaptation is claimed.
4. **Access observation is incomplete.** Trusted hooks distinguish access
   from movement, but sampled young bits and audited FD/mark paths can miss or
   coalesce accesses and timestamp observation rather than the exact access.
5. **A five-second tail is mandatory.** Live SHADOW/access capture must remain
   active for the full ranking horizon after the final candidate.  Ending the
   session at workload completion right-censors tail candidates; they cannot
   be relabeled as cold negatives.
6. **Probability ablation is not complete.** The old
   `analyze._train_weights()` path is only a legacy page-weighted, per-bin,
   smoothed 1-second log-odds heuristic (`LEGACY_1S_LOG_ODDS`).  It is not four
   independent 100 ms/500 ms/1 s/5 s classifiers and is not calibrated.
   `PROBABILITY_CLASSIFIER_ABLATION` and calibrated-Q15 `PRODUCT_ABLATION`
   remain `NOT_IMPLEMENTED`; a raw rank score must never substitute for
   `prob_q15`.
7. **APPLY remains unauthorized.** The validation helper can pass its
   Protect-only selection gate only when a hot-1 point exists, distinct hot-2
   validation evidence exists, and the 95% bootstrap CI for the upgrade
   selected-minus-baseline reuse-rate difference is entirely above zero.
   Bidirectional additionally requires a cold candidate whose Wilson
   downgrade-mistake upper bound is at most 0.10 and whose downgrade
   selected-minus-baseline bootstrap CI is entirely below zero.  These are
   threshold-selection prerequisites, not deployment authorization: real
   SHADOW model, coverage, trace-loss, KUnit, latency, refault, and App-tail
   gates are still required.  With no real dataset, both delivery gates remain
   false.  The ranking delivery gate is independent of the legacy one-second
   quadrant direction report and also fails closed on missing held-out
   pairwise support, non-vacuous monotonicity, quantized-order consistency, or
   fixed-native-tier/boundary residual discrimination.
8. **Validation session-cluster bootstrap is implemented.** Threshold
   selection deterministically performs 1,000 resamples, drawing whole
   validation sessions with replacement and deriving two-sided percentile
   95% intervals.  Difference intervals use only sessions containing both
   arms.  Its seed is derived from the operation tag and sorted paired-session
   identities.  Fewer than two paired supporting sessions or fewer than
   `max(200, 80% of requested)` valid resamples yields a null CI and a false
   direction/delivery gate.  Rates, coverage, Wilson counts, and delay
   summaries are base-page weighted through `folio_nr_pages`.  Unit fixtures
   do not turn these intervals into real evidence, and this validation
   procedure does not claim paired live confidence intervals.
