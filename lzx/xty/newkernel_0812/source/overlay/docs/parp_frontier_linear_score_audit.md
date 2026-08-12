# PARP frontier linear score audit

Status: `PARP_FRONTIER_SCORE_AUDIT_COMPLETE`

## Repository identity and safety

- Actual kernel repository/worktree:
  `/home/lzxxxxxx/桌面/huawei/huawei_mem/lzx/MGLRU-test/v4-parp/work/linux-6.17.13-parp-frontier-linear-score`
- Actual baseline: `2d37eac283ad7a5dc780f68064a50976c75df813`
- Branch: `feat/parp-frontier-linear-score`
- The new worktree was clean when created. No frozen worktree was modified.
- The outer `huawei_mem` repository has unrelated user changes. They are not
  part of this kernel worktree and were left untouched.
- No sudo, cgroup limit write, pressure workload, kernel installation, GRUB
  update, reboot, or APPLY action was performed during this audit.

## Phase2.10B history resolution

The two reported Phase2.10B heads are not competing histories:

```text
a707c01c967b0c283d4f341754f40d8ba5912741
  docs: enrich phase210b final report metadata
    |
9fdf54f0d72c728e8bc4c1e1074a3d6c0fee8d69
  fix: scope realism metrics to frozen selector
```

`9fdf54f0d` is the direct descendant of `a707c01c9`. Phase2.10C commit
`21ca1ac21` contains both. The selected baseline is the later verified
`2d37eac28`, which contains Phase2.10C plus the independent per-memcg tier-2
watermark/EWMA implementation required by the frontier design. This preserves
all Phase2.10B fixes and provides a waterline predictor to reuse rather than
creating a parallel implementation.

## Linux 6.17.13 MGLRU eviction call chain

The audited path is in `mm/vmscan.c`:

```text
try_to_shrink_lruvec()
  -> evict_folios()                         line 4721
       spin_lock_irq(&lruvec->lru_lock)     line 4739
       -> isolate_folios()                  line 4741
            -> scan_folios()                line 4711
                 -> sort_folio()            line 4614
                 -> isolate_folio()         line 4616
       spin_unlock_irq(&lruvec->lru_lock)   line 4748
       -> shrink_folio_list()               line 4753, without lru_lock
```

`sort_folio()` is at line 4463 and handles, in order, unevictable folios,
generation races/native promotion, tier protection, ineligible zones, and
dirty/writeback file folios. It returns false only for a folio that remains a
native isolation candidate. Therefore the only semantically valid insertion
point for the new score is between the false return from `sort_folio()` and
the call to `isolate_folio()`.

The whole `isolate_folios()`/`scan_folios()` walk runs while
`lruvec->lru_lock` is held with IRQs disabled. The score path therefore must
not allocate, sleep, perform I/O, call userspace, or acquire a mutex. A policy
promotion must update flags, `lrugen->nr_pages`, and the generation list under
this existing lock. `shrink_folio_list()` runs after the lock is released.

## Existing PARP modifications at the scan point

The baseline already calls `parp_adapter_score_folio()` at lines 4609-4612,
before native `sort_folio()`. That path is legacy observation code: its APPLY
adapter explicitly restores the native action and never changes MGLRU state.
It is not suitable for training or executing the final frontier policy because
it sees folios that native MGLRU later handles. The final implementation must
leave legacy compatibility observable but place frontier eligibility and
linear scoring after `sort_folio()`.

The scan-budget controller in `try_to_shrink_lruvec()` already provides:

- target/global reclaim scope classification;
- app binding and LSTM app-prior lookup through immutable RCU snapshots;
- pressure classification from reclaim priority;
- model/generation freshness checks and a circuit breaker;
- default observe behavior and a separately gated apply domain.

These inputs will be reused for App budget and safety checks. Global/root
reclaim will be Native bypassed in the first frontier version.

## Existing metadata and model publication

- File DAMON observations are aligned to immutable file-region evidence and
  looked up by domain, file identity and folio index.
- Anon evidence is currently aggregated per domain, not per folio.
- Evidence snapshots and app/binding snapshots are RCU-published and
  versioned. Model publication can reuse this RCU pattern.
- There is no existing per-folio PARP metadata and no existing PARP page_ext
  client. `struct page`/`struct folio` must not be enlarged.
- Native MGLRU already provides generation, generation birth timestamp,
  `LRU_REFS_FLAGS`, workingset state, folio size, type and zone under the LRU
  lock.
- Repeated-promotion suppression needs a per-folio epoch value. The safe
  design is an optional PARP page_ext client, with an explicit memory-cost
  report and Native bypass when page_ext metadata is unavailable.

## Frontier observability

The kernel has the information needed to compute a live per-lruvec/type
frontier under the LRU lock:

- `lrugen->min_seq[type]` and `max_seq`;
- `lrugen->nr_pages[gen][type][zone]`, separately for anon/file and NUMA node;
- `sc->nr_to_reclaim - sc->nr_reclaimed`;
- actual `isolated` and `reclaimed` pages returned by `shrink_folio_list()`.

No existing PARP API combines these values. The implementation should add a
small per-lruvec/type state and update a conservative reclaim-efficiency EMA
from actual pages, not a constant disguised as observed efficiency. Until at
least one valid efficiency sample exists, the frontier is invalid and the
runtime must report/bypass as `PARP_FRONTIER_NOT_OBSERVABLE`.

The historical traces do not contain the exact post-`sort_folio()` candidate
set, source sequence, live generation capacities, per-lruvec reclaim demand,
or actual frontier label. Consequently they cannot prove a real MGLRU
frontier SHADOW result. They can only be used for bounded proxy/replay
engineering checks, clearly labelled as such.

## Data audit

Available historical data includes:

- Phase2.8 real collection: three WPS and two FILES repeated sessions;
- Phase2.8B derived modeling data;
- Phase2.9A expert-routing data;
- Phase2.10B candidate data, whose candidate construction is not the final
  post-sort frontier universe;
- QQ train/validation attempts and one positive-support pilot. The frozen
  Phase2.10B split explicitly marks the QQ sample as pilot-only.

The requested minimum of four formal sessions for each of WPS, FILES and QQ
is not met. Phase2.10 V1/V2 candidate concatenation must not be reused as a
claim of final frontier performance. Any historical replay produced in this
work will preserve session boundaries, label proxies accurately, and cannot
upgrade the pilot to formal training data.

`workingset_refault_anon` and `workingset_refault_file` are real kernel
counters exposed through vmstat/memory.stat. Existing region traces and
future-access labels are not real refault events and must never be named as
such.

## Planned safe implementation boundary

1. Add a pure integer quantized-model reference and exhaustive vectors first.
2. Add a separate frontier mode whose default is OFF. SHADOW computes and
   traces but cannot alter a generation or isolation result.
3. Reuse AppBind/LSTM snapshot metadata and publish fixed-size models with RCU.
4. Compute frontier separately for each lruvec and anon/file type from actual
   capacity and an actual reclaim-efficiency EMA.
5. Add page_ext metadata only behind the PARP frontier configuration; account
   for its bytes per base page and bypass if unavailable.
6. Do not implement or enable APPLY unless genuine SHADOW data passes its
   safety and performance gates. Do not run controlled Apply A/B without
   explicit authorization.
