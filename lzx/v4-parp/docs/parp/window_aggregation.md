# Window aggregation

DAMON sampling and aggregation intervals remain attached to each sample.
PARP uses monotonic timestamps and 60 one-second buckets per logical region to
derive nested 10s/30s/60s access evidence and active-interval counts. Sample ID
deduplication is mandatory. Disorder up to two seconds is accepted; older
records are rejected and counted. File movement with identical logical offset
retains identity; anonymous VMA or epoch reconstruction does not.

