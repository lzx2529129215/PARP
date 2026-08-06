# Feature reproduction

Retained concepts are application prior, AppBind, TTL, model version, epoch,
MGLRU observation, trace, fallback, and control. Legacy dual Markov tables,
workload IDs, hints, and suggestion masks are not reproduced.

Phase 2.5 explicitly reimplements the LSTM-driven target-memcg scan-budget
controller with atomic full-prior batches, pressure/scope gates, default
Observe and guarded Apply. CONTINUE, REENTRY, dual-stage Markov, workload
hints, and CURRENT/NEXT suggestion masks remain replaced by PARP and are not
ported.
