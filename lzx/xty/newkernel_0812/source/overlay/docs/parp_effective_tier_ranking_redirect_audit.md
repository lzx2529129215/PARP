# PARP effective-tier pairwise-ranking redirect audit

Status: `PARP_EFFECTIVE_TIER_RANKING_REDIRECT_AUDIT_COMPLETE`

This audit records the in-place redirect requested after the effective-tier
work had already started.  It does not recreate the branch, discard a diff,
or alter the frozen frontier worktree.

## Repository and progress audit

- Worktree: `/home/lzxxxxxx/桌面/huawei/huawei_mem/lzx/MGLRU-test/v4-parp/work/linux-6.17.13-parp-effective-tier`
- Branch: `feat/parp-effective-tier`
- Audited HEAD: `a7447a42ad7331798f5203e2bd04a851df87190c`
- Effective-tier baseline: `a5ad006e8b66332a12e13f5e2dc7324bd6111d4a`
- Existing redirect-compatible commits:
  - `fcd2e9774 docs: audit MGLRU effective tier integration`
  - `41185bdd3 parp: add quantized global reuse scorer reference`
  - `a7447a42a parp: add controlled effective tier experiment harness`
- The kernel access-state, effective-tier gate, safety-budget, trace, and KUnit
  implementation is intentionally still an uncommitted work in progress.  The
  audit found no branch, worktree, or ancestry conflict and no reason to reset
  it.

At the redirect point, Phase A was committed; the Phase-B access-state and
metadata implementation and the Phase-C shadow/effective-tier implementation
were present but not yet committed; Phase-D validation was in progress; and
the original Phase-E offline analyzer still treated fixed-window reuse as its
main training target.  No live SHADOW or APPLY data had been collected.

## Changes retained without model-semantic changes

The following work is independent of whether the offline trainer uses a
fixed-window classifier or a pairwise ranker and is retained:

- the Linux 6.17.13 MGLRU source audit and the exact ordinary tier-gate
  integration in `sort_folio()`;
- `CONFIG_PARP_EFFECTIVE_TIER`, its mutual exclusion with the frontier
  experiment, default-off runtime mode, and separately gated experimental
  APPLY modes;
- the 24-byte/base-page `page_ext` reuse-history state and lifetime cookie;
- separation of trusted real access (`PTE_YOUNG`, audited
  `MARK_ACCESSED`, and successful `FD_REFERENCE`) from native generation,
  native tier, policy, putback, and list movement;
- the fixed six-feature integer lookup scorer.  Pairwise training still
  produces the same independently evaluable single-folio additive score;
- Q8 score-to-delta and effective-tier arithmetic, strict comparison, clamp,
  invalid-model Native fallback, special-protection preservation, +1/+2/+3
  upgrade ablation support, and the boundary-only -1 downgrade;
- OFF, SHADOW, PROTECT_ONLY, BIDIRECTIONAL, RANDOM_MATCHED, and RECENCY mode
  plumbing, independent upgrade/downgrade budgets, pressure/no-progress
  bypass, per-folio epoch claims, and race revalidation;
- candidate/access/outcome/batch/lock tracepoints, trace-loss contracts,
  exported-file-only collection, latency contracts, and the no-live-action
  experiment planner;
- the pure Python/C integer scorer parity tests and the effective-tier KUnit
  base.  Neither performs a sigmoid, candidate sorting, or online pairwise
  comparison.

The current kernel uses the neutral term `reuse_score`; it has no probability
field, probability calibration, runtime sigmoid, or runtime candidate sort.
The default lookup table remains an engineering/parity fixture until real
authorized SHADOW data trains and validates a ranker.

## Semantics that require adjustment

The following components are structurally reusable but must become
ranking-first:

| File | Existing element | Redirect |
|---|---|---|
| `tools/parp/effective_tier/collector.py` | future-window labels and right-censoring | preserve the four labels as auxiliary reports; add 5-second `next_reuse_delay_ns`, horizon/session censoring, and pair inputs |
| `tools/parp/effective_tier/contracts.py` | `PRIMARY_LABEL = reuse_within_1s`; three model ablations | add ranking horizon/tie contracts and five rank ablations; fixed-window labels cease to be the primary task |
| `tools/parp/effective_tier/feature_schema.json` | fixed-window label block | identify pairwise next-reuse ranking as the primary task and fixed windows as auxiliary monotonicity/AUC views |
| `tools/parp/effective_tier/analyze.py` | model selection and quality entrypoint | route the main output to bounded pair construction, Bradley–Terry training, quantization, ranking metrics, and validation-only score thresholds |
| `tools/parp/effective_tier/README.phase_e.md` | smoothed-log-odds trainer description | document pairwise training versus independent runtime scoring and retain probability classification only as an optional ablation |
| Phase-E schemas/tests | fixed-window completeness tests | retain them and add session-safe grouping, censoring, bounded sampling, reproducibility, quantization, and ranking-quality tests |
| experiment manifest | probability/product comparison wording | make ranker the mainline; mark the probability and calibrated-Q15 product paths optional/not implemented unless separately validated |

The exported v2 observation and model contracts use the canonical integer
score names `score_threshold_cold`, `score_threshold_hot_1`,
`score_threshold_hot_2`, and nullable experimental
`score_threshold_hot_3`.  The shorter `cold_threshold`/`hot_threshold_*`
keys remain only as deprecated, equality-checked compatibility aliases for
the runtime policy and older readers.  Neither spelling has probability
semantics.

## Probability-classification mainline being replaced

The old offline mainline is localized in
`tools/parp/effective_tier/analyze.py`:

- `_logit()` and `_train_weights()` fit per-bin smoothed fixed-window reuse
  log odds using `reuse_within_1s`;
- `_thresholds()` selects integer thresholds from validation fixed-window
  hit/mistake targets;
- `_quality()` makes ROC-AUC, average precision, binary NDCG, and reuse-rate
  buckets the central quality view;
- `train_ablations()` trains only the base, +native-tier, and
  +native-tier/+tier-idx fixed-window variants and exports them as the global
  candidate.

These functions and their tests are not kernel dependencies.  The existing
one-second smoothed-log-odds helper is isolated as `LEGACY_1S_LOG_ODDS`; it
does not select the default model, overwrite ranking output, or block the
ranking mainline.  It is not the requested four-window probability ablation.
The four `reuse_within_{100ms,500ms,1s,5s}` labels remain useful auxiliary
monotonicity and AUC measurements and therefore are not deleted, while
`PROBABILITY_CLASSIFIER_ABLATION` remains explicitly `NOT_IMPLEMENTED` until
four independent classifiers and validation calibration exist.

`PRODUCT_ABLATION` has no validated calibrated Q15 probability model in the
current work.  It remains `NOT_IMPLEMENTED`; a raw ranking score must never be
substituted for `prob_q15`.

## New primary contract

- Training task: bounded Bradley–Terry-style pairwise ranking of which folio
  reaches its next trusted real access sooner within the same reclaim context.
- Runtime task: one independent fixed-cost integer score per folio.
- Runtime decision: integer score threshold to Q8 virtual tier delta to the
  existing effective-tier gate.
- Score direction: higher means earlier expected next real access and higher
  short-term reuse value.
- Score interpretation: rank utility, not probability or refault probability.
- Runtime pair comparison, sigmoid, and candidate sorting: all disabled.
- Primary horizon: 5 seconds.  Default tie margin: 10 ms, with 0/10/50 ms
  offline sensitivity analysis.
- Primary group: same session, batch, and anon/file type.  The bounded fallback
  is same session, reclaim epoch, anon/file type, and a configured close-time
  window.  Cross-session pairs are forbidden.
- Sampling is deterministic and capped before materialization; session splits
  precede pair construction.  The strict default is 64 sampled pairs per
  group, the supported cap sensitivity set is exactly `[32, 64, 128]`, and
  per-App caps prevent one workload from dominating.
- The fallback context window is 100 ms within the same session, reclaim
  epoch, and anon/file type.  It is a grouping bound, not a reuse label.
- The event, labeled-candidate, observability, and pair-facing contracts are
  schema v2.  Session metadata and the fixed runtime feature schema remain
  v1 by design.
- The mainline ablations are exactly `rank_base`,
  `rank_plus_native_tier`, `rank_plus_native_tier_and_tier_idx`,
  `recency_only_rank`, and `recent_frequency_rank`.  Only `rank_base` is
  eligible for the current six-feature kernel shape.
- A trained export must carry per-threshold validation provenance, declare
  that the test set was not used for selection, and use the canonical scorer
  checksum projection.  A whole-artifact digest is recorded separately.

Because no authorized live SHADOW dataset exists, this audit records no pair
counts, learned quality, thresholds, or model selection result.  Those values
must remain null/unmeasured until exported real data is supplied.

## Safety disposition

No kernel was installed or booted; no cgroup setting was written; no pressure
workload or live mode was started; and no APPLY path was executed.  All
runtime-changing modes remain default-off and experimental-apply-gated.

`PARP_EFFECTIVE_TIER_RANKING_REDIRECT_AUDIT_COMPLETE`
