# PARP userspace tools

`parpctl.py` controls the safe debugfs mode. `model_pack.py` validates and
packs Q15 model values; `reference_model.py` is the bit-exact oracle used by
`evaluate.py`. Generic Netlink transport is intentionally not activated in
this safe-stub milestone.

Phase 2 adds `damon_collect.py` for non-privileged capability probing and
trace decoding, `region_schema.py` for safe half-open VMA alignment,
`dataset_export.py` for JSONL/CSV exports, and analysis tools for 10/30/60s
windows and data readiness. Export defaults redact real virtual addresses and
never writes paths, pointers, credentials, or tokens. Synthetic exports are
always labelled `SYNTHETIC_LEVEL1`, never runtime data.
