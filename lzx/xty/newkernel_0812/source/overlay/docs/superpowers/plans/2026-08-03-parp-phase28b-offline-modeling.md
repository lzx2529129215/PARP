# PARP Phase2.8B Offline Modeling Plan

1. Freeze and hash every fresh raw artifact; validate five sessions and 189
   operation instances.
2. Audit real schema and clocks, then stream deterministic 2s/5s/10s shards
   with atomic checkpointing.
3. Build sparse Top-K+OTHER V1/V2/V3/V4 kernel-only features and measure
   repeatability before model selection.
4. Select current-operation configuration on validation only, fit UNKNOWN
   thresholds there, and score untouched tests.
5. Predict next operation from causal model probabilities and kernel history;
   derive access-pattern weak labels solely from kernel trajectories.
6. Build observable Level-10/100/1000 future-segment labels, compare DIRECT,
   SEMANTIC and FUSED models, and calibrate safe thresholds on validation.
7. Run chronological causal replay and trace-based refault proxy simulations.
8. Rehash raw input, run all contracts, atomically complete state, and report
   measured gates without changing thresholds to force success.
