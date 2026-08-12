# PARP Phase2.9A Implementation Plan

1. Confirm the Phase2.8B baseline, clean worktree, immutable input roots, disk,
   dependencies, and absence/presence of local LearnedCache material.
2. Create the independent branch/worktree and a timestamped output tree.
3. Freeze the Phase2.8 raw manifest plus a Phase2.8B output manifest; write
   provenance and an atomic resumable state.
4. Add 32 contract tests before implementation and record the expected red run.
5. Implement causal candidate reconstruction, next-reuse/censoring labels,
   fixed-budget policy scoring, deterministic hashes, NDCG, AUC, and block
   bootstrap primitives; make the tests green.
6. Audit Phase2.8B score/ranking/selection degeneration without overwriting it.
7. Build 32/64/128/256 common candidate pools for the five sessions and compare
   the five proxy construction schemes.
8. Run Oracle sanity first.  If G0 fails, stop expert modeling and produce the
   integrity-tested gated report.
9. If G0 passes, build rule, K=3..8 data-driven, and hybrid workload taxonomies;
   select on train/validation only.
10. Train a global linear pairwise expert and workload-specific experts, then
    emit the complete true-workload by used-expert matrix and specialization
    statistics.
11. Train session-isolated current/future workload models; calibrate UNKNOWN and
    fallback on validation only; run hard/soft/full predicted routing.
12. Replay all policies with identical candidates/reclaim counts, measure
    latency, paired/block-bootstrap statistics, ablations, and causal online
    test-session predictions.
13. Run unit tests, compile checks, JSON/JSONL/schema validation, and raw plus
    Phase2.8B before/after hash verification.
14. Advance state atomically, create the three required research tables and
    final report, and make local scoped commits without push/reset/clean.

