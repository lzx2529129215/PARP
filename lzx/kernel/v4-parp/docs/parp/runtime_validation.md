# Runtime validation

Runtime collection requires the currently booted kernel to expose the Phase 2
PARP tracepoint, DAMON sysfs, and PARP debugfs. If any is absent, runtime is
`NOT_RUN_ENVIRONMENT_GATED`; synthetic/offline results are not promoted to
runtime PASS. Observe proof requires zero prefetch, anonymous pageout,
generation changes, scan-budget changes, and candidate skips.

