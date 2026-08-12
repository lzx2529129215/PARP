# PARP Phase 2.7 implementation plan

1. Freeze bootfix2 and Level3A hashes; locate runtime monitor, automation,
   operation predictor, app-scope configuration and live GUI capabilities.
2. Audit region/operation schemas.  Record the mandatory kernel trace gate and
   stop real-app collection on the currently running image.
3. Add failing unit tests for file identity/partitioning, 10/100/1000 mapping,
   weighted interval coverage, time windows, operations, future labels,
   leakage-safe splits, baselines, state transitions, Q15 and dry-run safety.
4. Implement small modules under `tools/parp/intra_app_prediction/`; keep schema,
   algorithms, dataset orchestration, modeling and CLI entry points separate.
5. Add backward-compatible region decoder support and a minimal PARP region
   trace extension with KUnit/source-contract tests.
6. Replay Level3A read-only.  Validate FILE/ANON parsing and windowing; mark
   FileId/segment generation gated rather than manufacturing missing fields.
7. Generate synthetic schema fixtures to exercise the complete dataset,
   split, model, metrics and dry-run pipeline before a rebuilt kernel exists.
8. Run pytest-compatible unittest discovery, py_compile, bash syntax checks,
   schema validation, focused KUnit/build checks and a full kernel build.
9. Do not install or reboot.  Leave a root collection runner and documented
   WPS/FILES session protocol ready for execution only after the rebuilt image
   boots.
10. Commit locally in reviewable units, preserve Observe/evidence-only state,
    audit old-output hashes, and report
    `PARP_PHASE27_KERNEL_TRACE_EXTENSION_REBUILD_REQUIRED`.
