# PARP Phase2.9A Workload-Adaptive Expert Design

## Scope and evidence boundary

Phase2.9A is a new ordinary-user, offline-only study rooted at Phase2.8B commit
`e4e7f2fd0`.  It reads the frozen Phase2.8 fresh trace and Phase2.8B derived
windows, but never mutates either.  Workload means a kernel-visible memory
access pattern, not an application or user operation.  Operation labels,
repeat identifiers, paths, names, contents, GUI state, and future state are
excluded from labels used online and from model inputs.

No local LearnedCache paper or source was present in the project audit.  The
design therefore maps only the task-specified temporal/spatial cache ideas to
fields that are demonstrably present in the frozen data.  Missing I/O or
writeback fields remain unavailable rather than being represented as observed
zeroes.

## Failure-first design

Phase2.8B produced identical DIRECT, SEMANTIC, and FUSED rankings.  Its broad
candidate set, deterministic ties, sparse future reuse, and protect-all safety
point made the trace proxy unable to distinguish policies.  Phase2.9A first
audits score, ranking, and selection hashes.  It then reconstructs a common
`MGLRU_ELIGIBLE_PROXY` candidate set from currently or recently observed,
version-valid Level-100 segments.  Candidate identity is metadata only.

Every decision freezes one candidate hash before any policy runs.  All policies
receive that exact list and reclaim the exact same count.  Ties use a causal,
identity-independent stable position established before scoring.  Metrics never
use truth as a tie breaker.

## Candidate and target semantics

The primary candidate scheme approximates a generation tail: among valid
segments observed in the current or recent six windows, prioritize inactive,
old, long-unreused segments and cap the pool at 128.  Comparisons also cover
all-observed, age-tail, recency-tail, oldest-N, and pool sizes 32/64/128/256.
This is explicitly a proxy, not the real MGLRU scan list.

For each decision and candidate, the label is the earliest observed active
state after the decision within 10/30/60/120 seconds.  A candidate with no
future observation is censored, never a negative.  Oracle ranking sorts real
next reuse time first and censored candidates last.  Reuse scoring and policy
simulation preserve fixed candidates and fixed reclaimed counts.

## Hard G0 gate

Before taxonomy or expert training, Oracle is compared with native-like
recency, DAMON hotness, and recent frequency at protection budgets 10/20%,
reclaim budgets 25/50%, and entries 8/16/32/64.  G0 passes only if at least one
primary non-zero reclaim point has at least 20% lower normalized refault proxy
than recent frequency, a distinct ranking hash, a nonempty reclaimed set, and
a protection set smaller than the candidate set.

If G0 fails, expert training is scientifically meaningless and stops.  The
pipeline still emits the required audit, fixed-candidate evidence, tests,
integrity proof, state, and a final report with
`PARP_PHASE29A_ORACLE_PROXY_INVALID`.

## Workload and expert path after G0

If G0 passes, three taxonomies are evaluated without operation labels:

- semantic kernel rules;
- deterministic k-means for K=3..8 on future reuse descriptors;
- rule initialization followed by support/stability/specialization merging.

Selection uses train and validation sessions only and considers support,
cross-session stability, reuse-distribution separation, cross-app presence, and
whether oracle-routed experts outperform a global expert.  Experts share one
identity-free feature schema and use a quantizable linear pairwise ranker.

The full path compares global, matched, single-best, majority, random, wrong,
oracle-routed, predicted hard, Top-2 soft, full-mixture, and fallback policies.
Current and future workload models use session-isolated splits; thresholds are
validation-only.  Online replay predicts before loading oracle workload or
future reuse.

## Statistics, latency, and claims

Policy comparisons are paired by decision.  The primary interval is a
deterministic session/time-block bootstrap, with naive window intervals reported
only as secondary evidence.  Holm-adjusted exploratory comparisons are marked.
Latency is measured for user-space offline inference and sorting only.  The
study can never claim real `workingset_refault` reduction or application
latency improvement.

## Self-review

- Candidate and budget equality are structural contracts, not report-time
  assertions.
- Oracle truth never enters expert features or predicted routing.
- Labels with no future observation are censored.
- Identical hashes can be called equal only, never better.
- Test sessions do not select taxonomy, hyperparameters, or thresholds.
- G0 prevents a large modeling pipeline from manufacturing conclusions from an
  invalid proxy.
- Phase2.8 raw/output hashes are frozen before Phase2.9A artifacts are written.

