# Phase E: offline effective-tier experiment harness

This appendix defines the Phase-E stopping point:
`PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED`. It adds contracts and offline tools;
it does not install or boot a kernel, read live tracefs/debugfs, write a cgroup,
start memory pressure, or enable any PARP mode. No real SHADOW or APPLY result is
implied by these files.

## Data boundary

`raw_event.schema.json` covers two exported event types:

- `tier_gate_candidate`: every folio that reaches the native MGLRU tier gate,
  including native-reclaim, native-protect, and special-native-protect folios;
- `real_access`: a strictly later `PTE_YOUNG`, `MARK_ACCESSED`, or
  `FD_REFERENCE` observation for the same experiment, session, folio cookie,
  and lifetime epoch.

Native generation movement, native tier promotion, and PARP policy movement
are not real-access labels. The labels describe future access, not refault.
True refault analysis uses exported `workingset_refault_file` and
`workingset_refault_anon` counter deltas in `observability.schema.json`.

For each candidate, `collector.py` creates four posterior labels:
`reuse_within_100ms`, `reuse_within_500ms`, `reuse_within_1s`, and
`reuse_within_5s`. A negative label is emitted only when its entire future
window was observed. A truncated window is `null`; a positive real access that
arrives before truncation is still a valid positive.

`session_metadata.schema.json` supplies the independently exported session
boundary, candidate counter, and trace-loss before/after measurement. Complete
tier-gate coverage is claimed only when:

1. the measured gate-counter delta equals the number of candidate records;
2. trace loss was measured from a named source; and
3. measured trace loss is zero.

Otherwise the rows remain available for diagnostics, but the collection and
training gate is marked incomplete.

## Offline flow

All input paths must point to previously exported ordinary files. `/sys`,
`/proc`, `/dev`, and `/run` paths are rejected.

`collector.py` also accepts `--trace-text` for an already-exported text dump of
the four `parp_effective_tier_{decision,access,outcome,batch}` tracepoints. It
normalizes the numeric kernel enums, joins outcome records, ignores explicitly
non-real access/movement events, infers an access session only when the folio
cookie/lifetime has one unambiguous owner, and imports measured batch model
time. It does not open a live trace source. Session metadata remains mandatory
because a per-record trace flag cannot prove trace-buffer loss.

```sh
python3 tools/parp/effective_tier/collector.py \
  --events exported-events.jsonl \
  --sessions exported-sessions.json \
  --output-dir offline-dataset

# Equivalent input adapter for a previously exported kernel trace text file:
python3 tools/parp/effective_tier/collector.py \
  --trace-text exported-effective-tier-trace.txt \
  --sessions exported-sessions.json \
  --output-dir offline-dataset

python3 tools/parp/effective_tier/analyze.py \
  --samples offline-dataset/labeled_candidates.jsonl \
  --telemetry offline-dataset/observability.jsonl \
  --output-dir offline-analysis

# Phase-F only: requires exported rows with the pressure trace fields. #lzx
python3 tools/parp/effective_tier/pressure_analysis-lzx.py \
  --samples offline-dataset/labeled_candidates.jsonl \
  --output offline-analysis/pressure_policy_ablation.json

python3 tools/parp/effective_tier/experiment_plan.py \
  --manifest tools/parp/effective_tier/experiment_manifest.template.json \
  --output-dir offline-plan
```

The plan renderer has no execute option and emits no shell command. Every live
matrix cell is marked `NOT_EXECUTED_PLAN_ONLY`.

## Required decisions and metrics

Special native protection is forced into the effective protected state and is
reported separately. The remaining native/effective combinations are:

| Native | Effective | Quadrant |
|---|---|---|
| reclaim | reclaim | `KEEP_RECLAIM` |
| reclaim | protect | `PREDICTIVE_UPGRADE` |
| protect | protect | `KEEP_PROTECT` |
| protect | reclaim | `PREDICTIVE_DOWNGRADE` |

Counts and rates are weighted by `folio_nr_pages`, not by folio records.
Upgrade hit/waste and downgrade mistake/cold precision are reported for all
four label windows. The offline bidirectional direction gate requires both:

- reuse among `PREDICTIVE_UPGRADE` is above `KEEP_RECLAIM`; and
- reuse among `PREDICTIVE_DOWNGRADE` is below `KEEP_PROTECT`.

The observability contract covers score/effective-tier/quadrant/batch timing,
`lru_lock` hold/wait/IRQ-disabled timing, direct and memcg reclaim, kswapd,
isolation and shrink timing, reclaim efficiency, VM/PSI/memory event deltas,
and App operation latency and failures. Analysis emits P50/P95/P99/P99.9/max
where raw latency samples exist.

## GLOBAL pairwise-ranking training and ablations

Splits are assigned to whole sessions. An explicit session split is preserved;
otherwise the experiment and session IDs are deterministically hashed into
70% train, 15% validation, and 15% test. Page-row random splitting is rejected.

The main trainer builds bounded same-context page pairs and minimizes an
offline Bradley--Terry logistic loss.  The sigmoid exists only in that loss.
The exported runtime shape remains one independently evaluable integer score
per folio: fixed bin lookups, one bias, checked addition, and score thresholds.
It is not a probability and the kernel never sorts candidates or compares
folios in pairs.

The target is the order of the next trusted real access within a 5-second
horizon.  The primary context is same session, batch, and anon/file type.  A
fallback context additionally requires the same reclaim epoch and candidate
times less than 100 ms apart.  Cross-session pairs and pairs of two
horizon-censored candidates are forbidden.  Session splitting always precedes
pair construction.

The default tie margin is 10 ms; 0/10/50 ms are constructed independently and
the main margin is selected only by validation pairwise accuracy.  Sampling is
deterministic, defaults to 64 pairs/group, and supports the explicit
32/64/128 cap ablations.  The manifest records the seed, available/skipped
pairs, App/page-type counts, and selected/default/supported caps.

Five exact GLOBAL ablations are trained:

1. `rank_base`: the six real-history features;
2. `rank_plus_native_tier`;
3. `rank_plus_native_tier_and_tier_idx`;
4. `recency_only_rank`;
5. `recent_frequency_rank`.

Only `rank_base` matches the version-1 six-slot kernel shape.  Shape
compatibility is not deployment eligibility: every offline artifact remains
unselected and not live authorized.  App, workload, session, identity, and
future fields are rejected as model inputs.

Cold/hot thresholds are selected only from validation sessions.  Coverage,
reuse rates, Wilson counts, and delay summaries are weighted in base pages by
`folio_nr_pages`; row counts remain separately labeled diagnostics.
Direction gates use a deterministic 1,000-resample whole-session cluster
bootstrap and compare arms only in sessions containing both arms.  Fewer than
two paired validation sessions or fewer than `max(200, 80% of requested)`
valid resamples makes the gate ineligible.  The downgrade path additionally
requires its Wilson 95% mistake-rate upper bound to be at most 0.10.
Experimental +3 has a separate, nullable validation threshold and is never
enabled for APPLY.

Held-out rank gates are independent of the auxiliary one-second quadrant
direction report.  They require at least 20 quantized test pairs with accuracy
strictly above chance, at least 20 non-tied quantization comparisons with
ordering consistency at least 0.95, non-vacuous score-bucket monotonicity,
all supported fixed-native-tier strata above chance, and at least 20 boundary
candidates with an actually observed next reuse and Spearman at least 0.10.
Missing evidence fails closed.
Trained export also checks `test_set_used = false` and per-threshold
`VALIDATION_SELECTED` provenance.

The old per-bin one-second smoothed-log-odds code remains isolated as
`LEGACY_1S_LOG_ODDS`.  It is not calibrated and cannot select or overwrite the
rank model.  The requested independent 100ms/500ms/1s/5s probability-model
ablation and calibrated Q15 product path are explicitly `NOT_IMPLEMENTED`.

## Outputs

The collector writes `labeled_candidates.jsonl`, `observability.jsonl`,
`collection_summary.json`, and `session_splits.json`. The analyzer writes:

- `tier_reclassification.json`;
- `upgrade_analysis.json` and `downgrade_analysis.json`;
- `dataset_stability.json` and `model_quality.json`;
- `ranking_dataset.json`, `pair_sampling.json`, `ranking_model.json`,
  `ranking_quality.json`, `score_distribution.json`,
  `score_reuse_monotonicity.json`, `threshold_selection.json`, and
  `ranking_quantization.json`;
- `probability_ablation.json`, explicitly non-mainline;
- `global_model.json`, an exact compatibility alias of `ranking_model.json`;
- `latency.json`, `lock_latency.json`, `reclaim_efficiency.json`,
  `app_latency.json`, and `vm_counter_deltas.json`;
- `summary.json`, which explicitly records that this tool ran no live SHADOW,
  APPLY, cgroup, or pressure action.

Run the Phase-E tests from the kernel tree root:

```sh
python3 -m unittest -v tools.parp.effective_tier.tests.test_phase_e
python3 -m compileall -q tools/parp/effective_tier
```

## Phase-F pressure-policy replay <!-- #lzx -->

`pressure_analysis-lzx.py` consumes only already-exported JSONL. It requires the
Phase-F fixed/binary/graded delta and local-pressure fields and replays the
fixed, binary-bypass, pressure-aware, recency-only, and rate-matched random
counterfactuals. The random arm is explicitly not exact-count matched. Future
trusted access remains a label for post-hoc evaluation, never a refault. Scan
amplification and reclaimable-candidate changes remain null unless the live
harness measured them independently. <!-- #lzx -->
