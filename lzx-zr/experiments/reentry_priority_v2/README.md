# Offline reentry priority decoupling

This directory implements the user's M2 experiment. Runtime and kernel files are outside its write scope. Execution stops after Gate-2 even if PASS.

- `build_dataset.py`: partition-local departure episodes; stale-after-return candidates excluded; all query weights per episode sum to 1.
- `model.py`: existing LSTM encoder, five monotonic survival logits; masked BCE only.
- `priority.py`: independent risk/coldness and RankOnly/RiskAwareRank mappers. Bin0 hot, Bin7 cold.
- `train.py`: full-query episode weighting or one uniformly sampled query per episode per epoch, including all-masked queries.
- `evaluate.py`: frozen B0/B1/B2 versus actual bin priorities B3/B4, episode-pair POA and first-decision-per-episode metrics; paired user-cluster bootstrap.
- `a11y.py`: frozen, continuous-score PC external validation with functional and executable-level process identities. Process prediction slots may collide; true identities never merge.
- `verify.py`, `test_contracts.py`: episode, censor, weighting, monotonicity, decoupling, and replay checks.
- `gate.py`, `report.py`: final offline decision and `/home/lzx/Desktop/PARP/FINAL-OFFLINE-REPORT.md`.

Run stages in the order listed in the final report. `finish.py` can wait for the two completed training statuses and run evaluation, A11y validation, verification, Gate-2 and reporting. It never starts a runtime/kernel stage.

`config.json` freezes thresholds, mappers, early stopping and strict zero-margin noninferiority before the accepted training runs. Existing LSApp test data have already been used for development. No claim of a new untouched holdout is made.
