# PARP Phase2.10B Implementation Plan

1. Audit branch, clean worktree, pilot provenance, and safety boundary.
2. Freeze raw/input hashes and create the Phase2.10B output state machine.
3. Reproduce legacy V1 and its full-universe positive-support diagnostic.
4. Implement causal inactive universe, independent labels, S1/S2/S3/S4,
   deterministic quota filling, checkpointing, and schema contracts.
5. Run support, realism, Oracle, and out-of-distribution model diagnostics
   without using QQ pilot labels for selector decisions.
6. Freeze the validation-selected selector, evaluate WPS/FILES/QQ test roles,
   run 70 legacy plus new tests, verify before/after hashes, and write final
   tables/report.

The implementation must stop with an explicit gated status whenever a hard
legality, input integrity, causality, support, realism, or Oracle requirement
is not met. No runtime collection is part of this plan.
