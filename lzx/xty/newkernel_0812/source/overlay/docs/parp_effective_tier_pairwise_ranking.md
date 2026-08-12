# PARP pairwise next-real-access ranking

## Primary offline task

The primary model learns a relative short-term reuse order:

> Given two folios from the same reclaim decision context, which one will be
> observed at its next trusted real access sooner?

This replaces fixed-window probability classification as the mainline task.
The exported single-folio utility is:

```text
reuse_score_i = bias
              + sum(weights[feature][bin(feature_i)])
```

Higher `reuse_score` means earlier expected next real access and greater
short-term protection value.  The score is ordinal.  It is not a probability,
does not promise calibration, and must not be written into a probability field.

Training uses a Bradley–Terry-style pairwise logistic objective:

```text
P(i earlier than j | training only) = sigmoid(reuse_score_i - reuse_score_j)

loss(i, j) = log(1 + exp(-label * (reuse_score_i - reuse_score_j)))
```

The sigmoid exists only in the offline loss.  Pair differences cancel the
single-page bias; the exported rank bias is therefore fixed to zero unless a
separately documented score-offset convention is introduced.  The learned
per-feature table can still score one folio independently.

The kernel consumes only quantized integer bias/bin weights and score
thresholds.  It never evaluates the sigmoid, compares two folios, or sorts a
candidate set.

## Why ranking is the primary target

A label such as `reuse_within_1s` makes every access at 1.001 seconds a negative
and every access at 0.999 seconds a positive, even though their reclaim value
is almost identical.  It also collapses a 5 ms reuse and a 900 ms reuse into
the same class.  Pairwise next-access ranking preserves the direction that
matters to reclamation: among comparable candidates, protect the one expected
to be needed earlier.

Fixed windows remain useful reporting views.  Reuse within 100 ms, 500 ms,
1 second, and 5 seconds is used for score-bucket monotonicity and auxiliary
ROC/PR AUC.  These views neither train the main model nor turn the score into a
probability.

## Candidate universe and feature time boundary

The candidate universe is every folio that reaches the native ordinary tier
gate in `sort_folio()`, including:

- native-reclaim candidates;
- native-tier-protect candidates;
- special Native protection candidates.

Special Native protection remains useful for observational ranking quality,
but is reported separately, is never predictively downgraded, and is excluded
from the default cold-downgrade training/threshold target.

Every feature is frozen at `candidate_time_i`.  No later access, action,
isolation, reclaim, putback, generation move, refault, session result, App
label, or workload label may enter the model features.  Identity joins require
the same experiment, session, folio cookie, and lifetime epoch.

Rows with `features_valid=false` retain their diagnostic candidate record but
cannot enter pairwise training.  Their six feature values remain `null`; the
ranking adapter rejects them rather than treating missing state as a cold
all-zero vector.

Valid next-access sources are audited real evidence such as observed PTE/PMD
young, explicitly audited mark-accessed sources, and successful FD/buffered
reference paths.  Native tier promotion, native generation movement, PARP
policy promotion, putback, list movement, and reclaim retry are not accesses.
The target describes a future access observation, not a true refault.

## Horizon and censoring

The primary ranking horizon is exactly:

```text
ranking_horizon_ns = 5_000_000_000
```

For candidate `i`:

```text
next_reuse_delay_i = next_real_access_time_i - candidate_time_i
```

The labeled-candidate schema records:

- `next_reuse_delay_ns`;
- `observed_within_horizon`;
- `censored_by_session_end`;
- `horizon_ns`;
- `tie_margin_ns`;
- `ranking_target_semantics =
  NEXT_REAL_ACCESS_DELAY_RIGHT_CENSORED_AT_HORIZON`.

Raw-event, labeled-candidate, and observability records use schema v2.  Session
metadata remains schema v1; the fixed runtime feature schema remains v1.

The rules are:

1. A trusted access within 5 seconds has its positive delay recorded.
2. If the full 5 seconds is observed with no access, the folio is right
   censored at the horizon (`+INF_WITHIN_HORIZON` conceptually); its numeric
   delay remains `null`.
3. An access later than 5 seconds does not become an exact primary delay; for
   this task the folio was not reused within the horizon.
4. If the session ends before the candidate's 5-second horizon and no earlier
   access was seen, `censored_by_session_end=true`.  The row is not a cold
   negative and cannot provide a pair ordering against an observed or
   horizon-censored row.

Live collection must therefore leave effective-tier decision and trusted
access tracing active for a full 5-second tail after the final candidate.  A
workload-end timestamp is not automatically a complete observation end.

## Session split before pair construction

Train, validation, and test splits are assigned to whole sessions before any
pair is constructed.  The pair builder rejects cross-session pairs and never
randomly splits candidate rows or already-built pairs.  This prevents the same
session context or folio lifetime from appearing in more than one split.

The GLOBAL training pool may combine WPS, FILES, and QQ sessions, but App,
process name, path, session ID, workload, and future data are reporting or
grouping fields only.  They are not ranker inputs.  Results must be reported
globally and by App, anon/file, native tier, and the
`native_tier == native_tier_idx + 1` boundary subset.

No session count is currently available from a real authorized export.  Train,
validation, and test session counts are therefore NOT COLLECTED, not zero.

## Pair groups

Pairs are constructed only inside one already-split dataset partition and one
comparable page type.  The exact context priority is:

1. **Primary group:** same experiment/session, same `batch_id`, and same
   anon/file type.
2. **Fallback group, used only when the primary group is insufficient:** same
   experiment/session, same `reclaim_epoch`, same anon/file type, and
   `abs(candidate_time_i - candidate_time_j) < 100_000_000 ns`.

The fallback close-time window is 100 ms.  It is a context bound, not a reuse
label window.  A pair is never formed across sessions, page types, or splits.
The grouping level used for each sampled pair must be present in the pair
manifest.

For two exact delays and a tie margin `m`:

```text
delay_i + m < delay_j  -> i ranks above j
delay_j + m < delay_i  -> j ranks above i
otherwise              -> tie; skip
```

The default is `m = 10_000_000 ns` (10 ms).  Sensitivity analysis must also
run 0 ms and 50 ms; validation chooses the primary margin and records it.

Censoring changes pair eligibility as follows:

| Candidate i | Candidate j | Pair rule |
|---|---|---|
| exact within-horizon reuse | exact within-horizon reuse | compare delays and skip ties |
| exact within-horizon reuse | full-horizon no reuse | exact reuse ranks higher |
| full-horizon no reuse | full-horizon no reuse | skip; order is unknown |
| session-end censored | any non-identical target | skip; order is not observed |

Pair orientation and label sign must be balanced or explicitly weighted.  A
feature swap must invert the label without changing pair eligibility.

## Deterministic bounded sampling

Generating all pairs from `N` candidates would be `O(N²)` offline and would
allow a large batch or one App to dominate.  The primary sampler therefore has
these canonical defaults:

```text
max_pairs_per_group = 64
seed                = "parp-rank-pairs-v1"
fallback_window_ns  = 100_000_000
```

The supported cap ablations are exactly `[32, 64, 128]`; 64 remains the strict
default, not a range.  Sampling is deterministic from the fixed seed and stable
group identity.  It is bounded before materializing all combinations and
stratifies eligible pairs to retain:

- clearly early versus clearly late exact reuses;
- within-horizon reuse versus full-horizon non-reuse;
- difficult non-tie pairs just beyond the selected margin.

The builder accepts caps through 128, and the manifest explicitly records the
selected value, strict default 64, and supported set `[32, 64, 128]`.  It also
uses a 250 ms boundary between its `hard_observed` and `wide_observed` strata,
and a default cap of 4096 sampled pairs per App per split.  Per-App and
anon/file caps or weights are recorded so no large group silently determines
the GLOBAL model.  Pair construction runs independently inside train,
validation, and test after the session split.

`pair_sampling.json` must report at least:

- original candidate count;
- eligible pair count without materializing an unbounded set;
- sampled pair count;
- ties skipped;
- double-horizon-censored pairs skipped;
- session-end-censored pairs skipped;
- primary versus fallback group counts;
- per-App and anon/file counts;
- positive and reverse-orientation label counts;
- cap, seed, fallback window, horizon, and tie margin.

No real pair manifest exists yet.  Candidate, eligible, sampled, tie, censor,
App, and orientation counts remain NOT COLLECTED.

## Five ranking ablations

All five variants use the same session splits, pair semantics, horizon,
sampling seed/caps, optimization protocol, quantization checks, and reporting
strata:

| ID | Model inputs | Purpose |
|---|---|---|
| `rank_base` | the six real-history runtime features | primary GLOBAL candidate; no native tier duplication |
| `rank_plus_native_tier` | Rank-Base plus `native_tier` | test whether native frequency adds stable residual signal |
| `rank_plus_native_tier_and_tier_idx` | Rank-Base plus `native_tier` and `native_tier_idx` | test both native state and dynamic boundary |
| `recency_only_rank` | `time_since_last_real_access_ms` | minimal recency baseline |
| `recent_frequency_rank` | `time_since_last_real_access_ms`, `consecutive_reclaim_candidate_count`, and `access_ema_q8` | recency plus simple past frequency |

Only `rank_base` has the current six-slot kernel shape.  The native-tier
variants are offline-only until an explicit feature-schema change is reviewed.
Every model artifact must emit its exact feature list; no variant may include
App/session/workload or future information.

Model selection is not based on training NDCG alone.  It prioritizes
cross-session stability, residual discrimination within a fixed native tier,
quantized ordering fidelity, fixed kernel cost, and conservative downgrade
risk.

## Training and quantization

The offline optimizer consumes binned feature-difference vectors:

```text
X_pair = X_i - X_j
label  = +1 when i should rank above j, else -1
```

It minimizes a numerically stable, regularized pairwise logistic loss with a
fixed seed and deterministic iteration order.  The result must be recoverable
as an independently evaluable single-page additive score.  A model that only
emits pair predictions is invalid for this project.

Floating-point training output is quantized into fixed bin weights and a zero
bias.  Export validation must cover:

- accumulator range and overflow rejection;
- score direction before and after quantization;
- Python floating score versus Python quantized score ordering;
- exact Python-quantized versus C/kernel integer score parity;
- quantized pairwise agreement;
- threshold equality and Q8 delta mapping.

Quantization may change score scale, but cannot reinterpret it as a
probability.  `ranking_model.json`/`global_model.json` must state:

```json
{
  "model_type": "pairwise_linear_ranker",
  "score_semantics": "higher_score_means_earlier_next_real_access",
  "score_is_probability": false,
  "runtime_pairwise_comparison": false,
  "runtime_sorting": false,
  "runtime_sigmoid": false,
  "quantized_bias": 0
}
```

No trained floating or quantized model exists from real data at this stopping
point.  The in-tree integer table is an engineering fixture and must not be
reported as a ranking result.

## Ranking quality and monotonicity

Primary held-out metrics are:

- pairwise accuracy/concordance;
- NDCG@K on each declared held-out evaluation population, with comparable
  decision-context views where sample size permits;
- Kendall tau or Spearman rank correlation where exact order is observed;
- censoring-aware C-index;
- quantized-versus-floating ordering agreement;
- residual discrimination after fixing native tier;
- score-bucket median next-reuse delay;
- score-bucket reuse within 100 ms, 500 ms, 1 second, and 5 seconds.

The desired monotonic direction is:

```text
higher score -> smaller median next-reuse delay
higher score -> higher reuse@100ms/@500ms/@1s/@5s
```

Auxiliary ROC-AUC and PR-AUC may treat the integer score as a ranking signal
for each fixed window.  They are not the primary objective and do not provide
probability calibration.

Metrics must be shown on untouched test sessions globally and by WPS, FILES,
QQ, anon/file, native tier, and the boundary-tier subset.  Thresholds and tie
margin are selected on validation only.  Bootstrap intervals must resample a
session-safe unit rather than individual page rows.

The delivery gate is fail-closed.  Held-out quantized evidence requires at
least 20 pairs and accuracy strictly above 0.5.  Quantized-versus-floating
ordering requires at least 20 non-tied comparisons and consistency of at
least 0.95.  Every native-tier stratum with at least 20 held-out pairs must
have accuracy strictly above 0.5; their pair-weighted and minimum accuracies
must also clear that floor.  The boundary subset requires at least 20
actually observed next-reuse samples and Spearman correlation of at least
0.10.  Score monotonicity
requires at least two distinct score buckets and known direction for every
adjacent median-delay and fixed-window reuse-rate comparison; an empty or
single-bucket comparison cannot pass.

There is no real held-out ranking result yet.  Pairwise accuracy, NDCG,
C-index, quantized agreement, monotonicity, fixed-native-tier discrimination,
and all per-App values are NOT EVALUATED and must be serialized as `null` or an
explicit not-evaluated status, never as fixture-derived quality.

## Validation-only score thresholds

Because `reuse_score` is not a probability, thresholds are integer score
cut-points selected only from validation sessions.  Candidate/observability
records use the canonical names `score_threshold_cold`,
`score_threshold_hot_1`, and `score_threshold_hot_2`.  The model artifact also
carries nullable `score_threshold_hot_3` for the separate experimental +3
contract; it defaults to `null` and is not a fourth primary threshold.  The
runtime policy/reference and candidate wire schema retain `cold_threshold`
and `hot_threshold_*` compatibility keys; integration exports equal-valued
canonical aliases and treats the shorter observation names as deprecated,
never probabilistic.

For native-reclaim candidates, the hot thresholds seek progressively smaller,
higher-score sets whose future-reuse rate and reuse-delay distribution improve
over `KEEP_RECLAIM`, while respecting upgrade coverage, waste, base-page
budgets, and lock-cost constraints.

For ordinary boundary protection
`native_tier == native_tier_idx + 1`, the cold threshold seeks a low-score set
with less future reuse and longer delay than `KEEP_PROTECT`.  It prioritizes
downgrade-mistake rate, the upper bound of its 95% confidence interval,
per-App P95/P99 risk, and verified native refault risk.  Strong and special
Native protection is never part of the downgrade action target.

Threshold reports must include score percentiles, base-page coverage, future
reuse rates, median next-reuse delay, per-App and anon/file strata, upgrade and
downgrade metrics, and bootstrap intervals.  Coverage, reuse rates, Wilson
counts, and medians are weighted by positive `folio_nr_pages`; candidate-row
counts are retained under explicitly named diagnostic fields.  Percentiles
alone are insufficient.

The threshold helper implements deterministic session-cluster bootstrap
intervals.  For each comparison it performs 1,000 resamples of whole
validation sessions with replacement.  Difference intervals use only
sessions containing both the selected and baseline arms, preventing
session-only arm confounding.  The pseudorandom seed is derived from the
operation tag and sorted paired-session identities, and the interval is the
2.5th to 97.5th percentile of valid resamples.  Fewer than two paired
supporting sessions, or fewer than `max(200, 80% of requested)` valid
resamples, produces a null CI, `gate_eligible = false`, and a false direction
gate.

The validation selection gates are deliberately strict:

- Protect-only requires a selected hot-1 point, distinct hot-2 validation
  evidence, and an upgrade selected-minus-`KEEP_RECLAIM` reuse-rate-difference
  bootstrap CI entirely above zero.
- Bidirectional first requires Protect-only, then a cold candidate whose
  Wilson 95% downgrade-mistake upper bound is at most 0.10 and whose downgrade
  selected-minus-`KEEP_PROTECT` reuse-rate-difference bootstrap CI is entirely
  below zero.

These are validation threshold gates, not live APPLY gates.  There is no real
dataset in the current delivery, so both delivery-gate booleans remain false;
unit-test fixtures cannot be reported as real gate evidence or paired-live
confidence intervals.

A trained scorer export additionally requires
`selected_on_split = validation`, `test_set_used = false`, and separate
`VALIDATION_SELECTED` provenance for cold, hot-1, and hot-2.  The aggregate
completion bit alone is never trusted.

The primary model artifact's `delta_tier_q8` and
`predictive_delta_tier_q8` maps contain exactly:

```text
score <= score_threshold_cold        -> -256 Q8 (-1 tier)
cold < score < score_threshold_hot_1 ->    0 Q8
hot_1 <= score < score_threshold_hot_2 -> +256 Q8 (+1 tier)
score >= score_threshold_hot_2       -> +512 Q8 (+2 tiers)
```

Thus the primary artifact has only `cold`, `neutral`, `hot_1`, and `hot_2`
delta entries.  Its default `experimental_plus3` object has
`status = NOT_SELECTED`, `default_enabled = false`,
`apply_supported = false`, and a null threshold.  A +3 boundary may be
recorded only after explicit validation selection with
`hot_threshold_3_selected_on_split = validation`; the exported experimental
object then records the same validation provenance.  It remains
offline/SHADOW-only and can never authorize APPLY.

The executable plan contract mirrors that boundary: primary
`max_upgrade_tiers_apply` is exactly `[1, 2]`, while
`experimental_plus3.max_upgrade_tiers` is `[3]`, its allowed modes are only
`SHADOW_EFFECTIVE_TIER` and `ORACLE_OFFLINE_ONLY`, and
`apply_supported = false`.

The kernel engineering fixture still implements distinct 96 and 144 hot-2
and hot-3 boundaries, with `max_upgrade_tiers = 2` by default.  This preserves
the +2 bin when an authorized experimental SHADOW replay raises the cap to 3;
with cap 1 or 2, the hot-3 bin retains that lower cap.  The mode setter rejects
APPLY when the cap is 3.  If hot evidence passes but cold evidence does not,
the valid threshold-selection result is Protect-only and Bidirectional
remains false.  If neither side passes, both selection gates remain false.

No validation-derived threshold currently exists.  The in-tree values -48,
48, 96, and 144 are engineering fixtures, not selected ranking thresholds;
in particular, 144 is not evidence of a selected +3 boundary.

## Refault feedback boundary

Future access and refault are different signals.  Candidate ranking labels use
trusted future access observations.  Native `workingset_refault_file` and
`workingset_refault_anon`, or verified workingset events, are required for
refault safety evaluation.

The native MGLRU refault PID and its `refaulted`, `evicted`, and `protected`
arrays remain unchanged.  A future independent rank-feedback structure could
stratify eviction, fast/late refault, policy protection/downgrade, and score
histograms by anon/file, native tier, and score bin/action.  That structure is
not implemented in the current tree.  Therefore no score-to-refault
monotonicity or adaptive-threshold claim is made.

If a later controller is authorized, it may adjust thresholds, Q8 deltas, or
global enable/disable in the background.  It must not train weights or mutate
native MGLRU PID data inside `lruvec->lru_lock`.

## Probability classifier is an optional, incomplete ablation

The former `analyze._train_weights()` implementation is a page-weighted,
per-bin, smoothed log-odds heuristic for `reuse_within_1s`.  Its accurate label
is `LEGACY_1S_LOG_ODDS`.  It is not a calibrated model and does not implement
the required four independent 100 ms, 500 ms, 1 second, and 5 second
probability classifiers.

Accordingly:

- `PROBABILITY_CLASSIFIER_ABLATION = NOT_IMPLEMENTED`;
- it is not the primary model and cannot overwrite ranking outputs;
- a future implementation must reuse the same session split, candidates, and
  features and report all four windows independently;
- `PRODUCT_ABLATION = NOT_IMPLEMENTED` until a separately validated and
  validation-calibrated Q15 probability model exists;
- a raw ranking score must never be used as `prob_q15`.

## Required artifacts and current evidence state

A real authorized run adds these ranking-first files to the timestamped output
directory:

```text
ranking_dataset.json
pair_sampling.json
ranking_model.json
ranking_quality.json
score_distribution.json
score_reuse_monotonicity.json
threshold_selection.json
ranking_quantization.json
probability_ablation.json
```

The first eight are mainline artifacts.  `probability_ablation.json` is
optional and must explicitly say `NOT_IMPLEMENTED` until the four-window
ablation exists.  `model_quality.json` must identify
`primary_task = pairwise_next-reuse_ranking`, `score_is_probability = false`,
`runtime_sorting = false`, and `runtime_sigmoid = false`.

At the current Phase-E boundary there is no authorized exported dataset or
trained model artifact.  The following are all unavailable: real session
counts, candidate/pair counts and App shares, learned weights, selected tie
margin, pairwise accuracy, NDCG, C-index, quantized agreement, monotonicity,
cold/hot thresholds, fixed-native-tier residual quality, four-quadrant sample
counts, and real Protect-only or Bidirectional evidence.  Metric values must
remain `null` or explicit `NOT_COLLECTED`/`NOT_EVALUATED` states;
`protect_only_gate_pass` and `bidirectional_gate_pass` remain false.  The
current model-artifact +3 threshold likewise remains null, with
`experimental_plus3.status = NOT_SELECTED`.

Live SHADOW collection, installation/boot, cgroup pressure, and every APPLY
mode require separate explicit authorization.  Until then the terminal state
remains:

`PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED`
