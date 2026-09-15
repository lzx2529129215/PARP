# Reentry Root-Cause Experiment Pack

Config: `config-path.txt` points to the frozen `outputs/reentry-root-cause-20260911-v1/configs/pack.json`.

Run from PARP root using the existing Python/PyTorch environment. For plotting, prepend `lzx-zr/experiments/slap_reentry/.deps` (Matplotlib 3.10.8) to PYTHONPATH. No runtime sink or kernel write path is imported or invoked.

Order: `audit.py`, `train_variant.py switch`, `temporal.py`, `sampling.py`, `build_original.py`, `train_variant.py original`, `eval_variant.py switch`, `eval_variant.py original`, `mapping.py`, `compare_original.py`, `class_prior.py`, `sampling_baselines.py`, `switch_effect.py`, `feature_collisions.py`, `report_inputs.py`, `report.py`, `verify_artifacts.py`. Run `test_pack.py` after `mapping.py` has created the holdout manifest. Each script writes its own artifacts; redirect console output to a separate log per stage. Keep heavy training sequential on this host; resource contention affects wall time, not configured epochs.

The existing mixed mapped M1 is reused, not reselected on development results. Switch-only changes only training selection; validation stays fixed. The raw-space run uses exact paired mapped-schedule anchors to control sampling. It retains all 87 source identities. Matrix dimensions necessarily scale with vocabulary; architecture widths are fixed. Mapped and raw candidate sets differ; an additional one-to-one shared-candidate comparison checks equal ground truth.

Observed next-entry episode deduplication does not imply statistical independence. Censored groups are separate. Episode-pair balanced POA averages repeated predictions within each identifiable next-entry pair before pooling pairs; uncertainty is bootstrapped by user. Argmax and probability mass are compared on the exact same valid-label support.

Simple medians fit completed training episodes once. Last/EMA use only completed past intervals strictly before query. Both literal interval scores and elapsed-age-adjusted remaining heuristics are reported; neither future labels nor test medians fit their parameters. Alpha remains 0.5.

The old test is development. Retrospective 60/15/15/10 session partitions are reserved as metadata and crossing sessions quarantined, but prior exposure cannot be undone. A genuinely untouched final holdout is explicitly pending unseen data. This pack stops after ROOT-CAUSE-REPORT.md: no next model, PC collection, runtime or kernel stage runs automatically.

Execution note: models run with one CPU thread on the contended two-vCPU host. An identical-batch check measured a 12–16x forward/backward slowdown with two threads; maximum output discrepancy was 5.96e-7. The stopped mmap/two-thread attempts are archived under logs/. Exact selected rows are compacted in memory without changing minibatch order. Learning hyperparameters and model structure are unchanged.

Supplemental count-only/read-only diagnostics: `class_prior.py` precedes `sampling_baselines.py`; `switch_effect.py` follows switch evaluation; `feature_collisions.py` measures exact encoded-input conflicts. `verify_artifacts.py` follows the final report and checks best-epoch selection plus 128 replay rows per new checkpoint. `report_inputs.py` reproduces user coverage and one-to-one label parity, asserting equality if their metrics already exist. Use a new output config for any new experiment; do not overwrite the frozen completed pack.
