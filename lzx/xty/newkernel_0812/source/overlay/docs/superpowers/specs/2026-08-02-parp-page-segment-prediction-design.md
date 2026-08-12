# PARP Phase 2.7 multi-resolution page-segment prediction design

## Scope and invariants

Phase 2.7 adds an offline dataset and dry-run predictor for intra-application
file segments and anonymous working sets.  The existing duration-aware LSTM
continues to decide only how much a memcg may scan.  This predictor decides
which logical file segments are likely to be accessed in 10/30/60 seconds and
whether the anonymous working set is cooling.  It never writes a kernel
control interface, changes reclaim, prefetches a file, or pages out anonymous
memory.

The model is `PageAccessState_t -> PageAccessState_t+1`.  The removed
CONTINUE/REENTRY and CURRENT/NEXT suggestion-mask semantics are neither inputs
nor outputs.

## Runtime evidence contract

Every FILE observation must carry an immutable logical identity and bounds:

```
FileId = (dev_major, dev_minor, inode, file_version)
PageRange = [file_page_start, file_page_end_exclusive)
Partition = (file_page_count, partition_generation, file_size_at_partition)
```

It also carries sample/event time, PID/TGID, domain and AppBind metadata,
sampling/aggregation intervals, age, access evidence, and the PARP sample ID.
ANON observations retain only session-scoped identity: domain, foreground
epoch, mm cookie, VMA signature, and relative page range.

The bootfix2 trace is insufficient: it omits device, inode, file version and
file size.  Native DAMON virtual ranges cannot be reliably joined to an
asynchronous, split PARP sample stream.  Therefore this worktree minimally
extends the existing trace payload.  A rebuilt kernel is required before any
WPS/FILES collection can be accepted as training data.  No core reclaim or
PARP policy semantics change.

## File partitions

Requested resolutions are 10, 100 and 1000.  For a nonempty file:

```
effective_bins = min(requested_bins, file_page_count)
segment_id = min(effective_bins - 1,
                 page_index * effective_bins // file_page_count)
start_page = ceil(segment_id * file_page_count / effective_bins)
end_page = ceil((segment_id + 1) * file_page_count / effective_bins)
```

The implementation uses quotient/remainder arithmetic so the mapping does
not require an overflowing multiplication.  A central function owns both
mapping and boundary calculations.  Size or version changes allocate a new
partition generation; old predictions cannot be applied to it.

Level 10 and 100 are dense.  Level 1000 is sparse and stores active segment
IDs and values plus the complete effective-bin count and negative-candidate
space.  `NOT_OBSERVED`, `OBSERVED_INACTIVE`, and `OBSERVED_ACTIVE` are distinct.

## Time and coverage

Training uses epoch-aligned, nonoverlapping 10-second windows based on event
time.  Online rolling windows remain a separate runtime concept.  Every
record retains event time, collection time, snapshot publication time,
timezone, boot/session/run identity.

Coverage is the union length of accessed logical page ranges divided by the
segment page count.  Weighted coverage uses a sweep line: ranges are clipped
to a segment; at each elementary interval the maximum active ratio among
overlapping samples is integrated.  This prevents duplicate or overlapping
DAMON regions from making coverage exceed one.  Active ratio is normalized by
`ceil(aggregation_interval / sample_interval)`.

Continuous coverage, weighted coverage and intensity are primary features.
The 50% and 80% flags and NONE/SPARSE/MEDIUM/DENSE/VERY_DENSE state are retained
for audit and ablation.

## Anonymous memory and operations

ANON output is a per-domain ten-second summary: total/observed/hot/warm/cold
bytes, region counts, access-ratio and age statistics, working-set delta,
recent activity, cooling, and unresolved ratios.  Optional VMA subdivision is
disabled and never crosses boot, mm-cookie or foreground-epoch boundaries.

Automation `OP_START` and terminal events are normalized into operation ID,
app, start/end nanoseconds, type, source, confidence and session.  Alignment
chooses the longest overlap as the dominant operation and assigns PURE at
>=80%, MIXED at >=50%, otherwise LOW_CONFIDENCE.  Future operations never enter
current-window features.

## Labels, splits and predictor

Each segment has future 10/30/60-second access, max coverage and weighted
coverage labels plus explicit availability flags.  Missing tail horizons are
unknown, never zero-filled.  Splits are session/time ordered with a 60-second
purge gap; vocabulary, normalization and clustering fit training only.

First-version models are Last-window, Recent-frequency, Global-frequency and
a Page-Access State model.  The latter uses deterministic feature encoding,
configurable KMeans/MiniBatchKMeans-style clustering and duration-aware state
transitions.  A lightweight built-in implementation is used because the
guest has no NumPy or scikit-learn and dependencies are not downloaded.

Dry-run output contains versioned FileId/partition data and Q15 probabilities.
Generation is persistent and increasing; unknown files remain UNKNOWN.  The
only supported online mode is `--no-kernel-write`.

## Privacy and provenance

Absolute paths are excluded from public tables.  The dictionary stores a
precomputed SHA-256 path hash and optional basename; collection policy may
salt that hash without changing the schema.  Raw evidence, build configuration,
boot/session IDs, input hashes and immutable source HEAD are retained.  The
Level3A fixture is read-only and labeled `RUNTIME_LEVEL3A_REPLAY_FIXTURE`; it is
never mixed with future WPS training data.
