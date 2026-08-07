# Data collection

`damon_collect.py --probe` reports whether the running kernel exposes DAMON
sysfs, PARP debugfs, and the aligned-region tracepoint without changing system
state. Trace decoding and dataset export use JSONL/CSV schema v1. Real virtual
addresses are normalized/redacted by default, paths are omitted, and no kernel
pointers, tokens, credentials, or content are logged. Controlled fixtures are
labelled `SYNTHETIC_LEVEL1`.

