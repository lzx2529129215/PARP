# PARP Phase2.10 App-Specific Reclaim Design

## Research boundary

Phase2.10 asks whether a stable foreground application identity can select an
application-global reuse ranker that improves a bounded MGLRU candidate order.
It does not revive the unsupported Phase2.9A workload-expert taxonomy.  App
identity selects a model; it is never a model feature.

The five frozen policies are Native, Generic cross-app, matched app, fixed
wrong-app (`WPS→FILES`, `FILES→QQ`, `QQ→WPS`), and matched app with
`specific→generic→native` fallback.  Every comparison must reuse the exact
candidate identities and reclaim count.

## Trusted baseline

The baseline is commit `0ddb95193359324b7d538f72fdf539e2ef849cf1`.
Phase2.9A showed a 64.4% improvement of one global ranker over recent-frequency
on a trace proxy, while Oracle workload routing added zero.  This is neither a
real workingset-refault result nor an application-latency result.

## Data and leakage boundary

Training, validation, and final A/B sessions are disjoint whole sessions.  A/B
records never participate in fitting, scaling, feature selection, calibration,
or threshold choice.  Candidate features are causal reuse intervals, EMA,
ages, coverage, spatial position, size, generation/tier and genuinely available
page state.  App ID, operation, automation step, session/repeat identity, paths,
names and content are forbidden.

At least one independent train and validation session is required for each app
before a four-model cross matrix is meaningful.  Missing QQ data is a hard
gate: no synthetic or copied QQ model may stand in for a real QQ-specific
ranker.

## Routing and fallback

`AppBind` resolves foreground app to test subdomain and model snapshot.  A
specific snapshot is accepted only when model/schema versions, feature
availability, confidence, app stability, finite output, scoring deadline and
TTL pass.  Otherwise routing falls back to Generic, then Native.  Native never
runs a model.  Fault injection is isolated and may not alter candidate count or
escape the test parent cgroup.

## Mixed scenario and privacy

The future mixed scenario uses generated fixture documents, a dedicated file
tree, and a test QQ account or offline mock.  It interleaves WPS editing,
FILES navigation and QQ read-only/test-conversation actions using frozen seeds
and nonconstant dwell periods.  It must not access normal home directories,
real documents, real chat history or real contacts.  The existing auto-login
scenario is inventory evidence only and is not approved for execution.

## cgroup, pressure, Observe and Apply

All target apps and pressure workers belong to one test-only parent with
separate WPS/FILES/QQ/pressure child scopes.  No limits are selected until a
no-pressure pilot measures `W_peak`, `W_p95` and `W_steady`.  Any write to
`memory.high`, `memory.max`, pressure start, Observe collection requiring
privilege, or Apply requires explicit authorization.

Apply, if later implemented, can only reorder a finite MGLRU candidate list.
It cannot scan extra pages, alter reclaim count, wait for Python, prefetch,
pageout anonymous memory, or affect another cgroup.  Without a bounded kernel
scorer or precomputed snapshot, the project remains Observe-only.

## Gates

G0 requires all learned rankers to beat Native/Recent on held-out trace proxy.
G1 requires matched models to beat Generic and wrong models for at least two
apps with paired block-bootstrap CI above zero.  QQ insufficiency precedes G0
and G1.  G2–G8 cover live Observe, real normalized refault, specialization,
fallback, latency and safety and require separately authorized runs.

The current safe stage must stop at
`PARP_PHASE210_QQ_MODEL_DATA_INSUFFICIENT` if inventory confirms no QQ train and
validation data.  No downstream result may be fabricated.

## Stage-3 data amendment (2026-08-03)

Two authorized QQ Observe sessions now exist, but row count alone is not model
adequacy.  The frozen whole-session roles are WPS `wps_01` train, `wps_02`
calibration audit, `wps_03` evaluation; FILES `files_01` train and `files_02`
evaluation; QQ `qq_train_01` train and `qq_validation_01` evaluation.  Future
mixed/A-B sessions remain excluded.

QQ is converted directly from the common `parp_region_evidence` schema into
10-second, generation-tail, 128-candidate decisions.  Missing QQ kernel-metric
samples are recorded as unavailable and are not synthesized.  A split is
adequate only with at least 20 positive train candidates, 20 positive
evaluation candidates and 10 pairwise-evaluable evaluation decisions.  This
is a data-support gate, not a performance threshold.  A zero-positive
evaluation split must stop before specialization inference even if millions of
raw region rows exist.
