# PARP frontier linear score implementation report

## Outcome and safety boundary

The OFF/SHADOW implementation, reference model, observability, tests, and a
complete kernel build are ready. Historical data cannot support a genuine
post-`sort_folio()` frontier replay, so the overall data-gate result is:

`PARP_FRONTIER_SCORE_SHADOW_NOT_SUPPORTED`

Phase F APPLY was deliberately not implemented. Writing mode `2` returns
`-EOPNOTSUPP`, `apply_compiled` is 0, and no generation can be changed by this
feature. No sudo/root command, cgroup limit write, pressure workload, kernel
installation, GRUB change, reboot, live SHADOW, or APPLY action was performed.

The phase results are:

| Phase | Result |
|---|---|
| A, repository audit | `PARP_FRONTIER_SCORE_AUDIT_COMPLETE` |
| B, integer reference | `PARP_QUANT_MODEL_REFERENCE_PASS` |
| C, kernel OFF/SHADOW | `PARP_FRONTIER_SCORE_SHADOW_IMPLEMENTED` |
| D, regression/build | `PARP_FRONTIER_SCORE_BUILD_PASS` |
| E, real replay gate | `PARP_FRONTIER_SCORE_SHADOW_NOT_SUPPORTED` |
| F, APPLY implementation | Not entered because Phase E did not pass |
| G, controlled A/B | `PARP_FRONTIER_SCORE_APPLY_AUTH_REQUIRED` |

## Repository identity and baseline decision

- Repository/worktree: `/home/lzxxxxxx/桌面/huawei/huawei_mem/lzx/MGLRU-test/v4-parp/work/linux-6.17.13-parp-frontier-linear-score`
- Branch: `feat/parp-frontier-linear-score`
- Baseline: `2d37eac283ad7a5dc780f68064a50976c75df813`
- Kernel code HEAD used by the final build:
  `02f38bfcb233736b6842de8832231793c502f7ea`
- The dedicated worktree was clean when created; frozen worktrees and unrelated
  user modifications in the outer repository were not touched.

The reported Phase2.10B heads are not competing baselines. Git ancestry shows
that `9fdf54f0d72c728e8bc4c1e1074a3d6c0fee8d69` descends from
`a707c01c967b0c283d4f341754f40d8ba5912741`; Phase2.10C contains both. The
selected later baseline also contains the independent per-memcg tier-2 EWMA
watermark implementation. The detailed evidence is in
`docs/parp_frontier_linear_score_audit.md`.

## Modified files

```text
docs/parp_frontier_linear_score_audit.md
docs/parp_frontier_linear_score_implementation.md
include/linux/mmzone.h
include/linux/parp.h
include/trace/events/parp.h
mm/page_ext.c
mm/parp/Kconfig
mm/parp/Makefile
mm/parp/control/debugfs.c
mm/parp/core/frontier_score.c
mm/parp/internal.h
mm/parp/tests/parp_test.c
mm/vmscan.c
tools/parp/frontier_score/.kunitconfig
tools/parp/frontier_score/README.md
tools/parp/frontier_score/__init__.py
tools/parp/frontier_score/benchmark.py
tools/parp/frontier_score/cbench.c
tools/parp/frontier_score/cscore.c
tools/parp/frontier_score/default_models.json
tools/parp/frontier_score/reference.py
tools/parp/frontier_score/replay_audit.py
tools/parp/frontier_score/tests/__init__.py
tools/parp/frontier_score/tests/test_reference.py
tools/parp/frontier_score/tests/test_replay_audit.py
```

## Actual MGLRU call chain and lock range

The audited Linux 6.17.13 path is:

```text
try_to_shrink_lruvec()
  -> evict_folios()
       spin_lock_irq(&lruvec->lru_lock)             mm/vmscan.c:4751
       -> isolate_folios()
          -> scan_folios()
             -> native sort_folio()                 mm/vmscan.c:4621
             -> parp_frontier_consider()            mm/vmscan.c:4625
             -> native isolate_folio()              mm/vmscan.c:4626
       spin_unlock_irq(&lruvec->lru_lock)           mm/vmscan.c:4760
       -> shrink_folio_list(), without lru_lock     mm/vmscan.c:4768
       spin_lock_irq(&lruvec->lru_lock)             mm/vmscan.c:4796
       -> parp_frontier_feedback()                  mm/vmscan.c:4797
       spin_unlock_irq(&lruvec->lru_lock)           mm/vmscan.c:4816
```

`parp_frontier_prepare()` is called at `mm/vmscan.c:4593-4597` and scoring is
only called after native `sort_folio()` returns false. Thus unevictable,
generation-race/native promotion, tier protection, zone, and dirty/writeback
handling retain native precedence. SHADOW always proceeds to the unchanged
native `isolate_folio()` result.

The candidate path runs under the existing `lruvec->lru_lock` with IRQs
disabled. `mm/parp/core/frontier_score.c:540` therefore uses bounded loops,
fixed arrays, atomics, RCU reads, and page_ext lookup only: no allocation,
sleep, I/O, userspace call, floating point, sort, or mutex. Runtime OFF is a
disabled static-key branch. The mode mutex is only used in the debugfs control
path, outside reclaim.

## Features and storage

The optional `CONFIG_PARP_FRONTIER_SCORE` selects PAGE_EXTENSION and adds a
page_ext client rather than enlarging `struct page` or `struct folio`.

| Feature | Kernel source/update | Storage/type | Causality and applicability | Lock requirement |
|---|---|---|---|---|
| access age | `lru_gen_update_size()` access/promotion hook; read at scan | `last_access_ns`, atomic64 | past access only; anon/file | access path or lru lock |
| previous access interval | delta between two access hooks | atomic u32 ms | past intervals; anon/file | atomic |
| access EMA | Q8 EMA at access, decayed at scan | atomic u32 | past events; anon/file | atomic |
| reuse interval EMA | 1/4 integer EMA at access | atomic u32 ms | past events; anon/file | atomic |
| inactive count | reset on access, saturating increment at candidate scan | atomic u32 | current/past scans; anon/file | atomic |
| generation distance | `max_seq - source_seq` at scan | transient s64 | current MGLRU state; anon/file | lru lock |
| time in generation | timestamp on observed generation change | atomic64 ns, emitted as ms | past residency; anon/file | atomic/lru lock |
| App reentry | versioned PARP App context/LSTM prior | transient Q15 u32 | low-frequency App signal; not App ID | RCU snapshot/lru lock |

The metadata object is 48 bytes after alignment per base page when this
non-default Kconfig feature is compiled in. On this 15.6-GiB VM that is about
187 MiB; on 16 GiB it is about 192 MiB. This is the largest static memory cost.
With the default Kconfig value `n`, the client and cost are absent. Missing or
uninitialized metadata causes Native fallback.

## Quantized models

Schema v1 has 8 features and 6 bins per feature. Bin edges are signed 64-bit
continuous read-only arrays, weights are signed 16-bit arrays, and accumulation
is signed 32-bit. Equality stays in the lower bin. Fixed WPS, QQ, FILES and
Generic tables are RCU-addressed and version checked. Routing is App-specific,
then Generic, then Native; App ID is not a model feature. The score is a reuse
score/logit, not a calibrated probability.

The tables are deterministic engineering/test tables, not trained production
models. They do not concatenate the Phase2.10 V1/V2 candidates. Their JSON
SHA-256 is
`331db112f81d2f1d78519cdea7e1ed5247b650d1ba935ff20f3618376064f8ee`.

## Frontier, demand, budgets, and fallback

`parp_frontier_prepare()` (`frontier_score.c:372`) operates per
`lruvec = memcg x node` and separately for anon/file. It takes remaining live
demand as `max(0, sc->nr_to_reclaim - sc->nr_reclaimed)`. That demand is the
same target-memcg reclaim request produced by the existing independent tier-2
EWMA/watermark controller; this code does not create a second waterline.

For each eligible generation and zone up to `reclaim_idx`, it reads live
`lrugen->nr_pages`, multiplies by the conservative Q15 EMA of actual
`reclaimed / isolated`, and selects the first generation whose cumulative
effective capacity covers demand. Anon/file feedback is updated from the real
`shrink_folio_list()` result. With no actual efficiency sample, insufficient
capacity, a changed source sequence, or an expired 50-ms context, it bypasses.

Only candidates whose source sequence equals the computed frontier sequence
are scored; projected fully consumed generations stay Native. Four budgets are
the minimum of App/LSTM budget, 128-base-page batch budget, 512-base-page epoch
budget, and frontier headroom. `folio_nr_pages()` is used throughout. The
page_ext epoch marker suppresses repeated selection in one epoch.

Other Native fallbacks include OFF, root/global or non-target memcg reclaim,
priority <= 2, no AppBind/context, invalid model/schema/version, invalid
features, no demand, budget exhaustion, and any stale frontier. The current
SHADOW code records `would_promote`; it never changes folio flags, lists,
generation, accessed/reference evidence, or isolation.

## Modes and observability

- OFF (default): static key disabled; Native behavior.
- SHADOW: validates frontier, scores, accounts budgets, traces
  `would_promote`, and still always calls native isolation.
- APPLY: enumerated for ABI clarity but deliberately not compiled; setter
  returns `-EOPNOTSUPP`.

Debugfs files are `/sys/kernel/debug/parp/frontier_score_mode` and
`frontier_score_stats`. The tracepoint `parp_frontier_decision` includes time,
domain, App/model, node/type, mode, epoch/batch/foreground epoch, random-keyed
SipHash folio cookie, source/frontier sequence, demand/headroom and all budgets,
folio base pages, score/threshold, schema/model, reclaim efficiency, score
duration, reason, `would_promote`, and `applied=false`. It exposes no path,
content, raw pointer, or PFN.

## Verification

### Python and C

- Python compile: PASS.
- Regression suites: 399/399 PASS (16 frontier, 84 intra-App, 41 core,
  25 Phase2.8, 31 Phase2.8B, 32 Phase2.9A, 70 Phase2.10, 46 Phase2.10B,
  54 Phase2.10C).
- Python/C score consistency: PASS for at least 240 deterministic vectors,
  including bin boundaries and extreme values.
- ASan + UBSan standalone C oracle: PASS.
- TSan: ENVIRONMENT_BLOCKED at link because host `libtsan_preinit.o` is absent.
- CTest: NOT_APPLICABLE; this kernel project has no CMakeLists and the host has
  no `ctest` executable.

### KUnit and static/build validation

Three frontier KUnit cases cover scoring/routing, frontier/budgets/TTL/source
sequence/APPLY rejection, and trace schema. Their test object and all affected
objects (`frontier_score.o`, `parp_test.o`, `vmscan.o`, `page_ext.o`, and
`debugfs.o`) compile with `W=1`.

UML KUnit runtime is ENVIRONMENT_BLOCKED by the host UML headers
(`__NR_close_range` undeclared); QEMU is not installed. This is not reported as
a KUnit runtime pass. The product build disables `CONFIG_PARP_KUNIT_TEST`
because an older unrelated DAMON KUnit case requires a different config, while
the new KUnit object was compiled separately.

`git diff --check`, checkpatch (0 errors, 0 warnings), strict binary patch
`git apply --check` against the baseline, and affected-object W=1 compilation
all pass. Sparse and clang are unavailable on this host.

### Complete kernel build

The final code HEAD was built externally in `/tmp/tmp.4d9V2jWMkh`, without
installing it:

- kernel release: `6.17.13-parp-frontier-shadow-00102-g02f38bfcb233`;
- config: PARP=y, PARP_FRONTIER_SCORE=y, PAGE_EXTENSION=y, KUnit test disabled;
- `bzImage`: PASS, 14,033,920 bytes, SHA-256
  `74edabc2103251843acb2c23009d51b85aac08e270ca8a8d43e81699898c7580`;
- `vmlinux`: 50,400,336 bytes, SHA-256
  `8c259ef39db8c925dafc248af954b7d5ce3eb9847b463b4d71976a358e3f7fcc`;
- separate `make modules`: PASS.

### No-reclaim microbenchmark

The optimized standalone C scorer used `CLOCK_MONOTONIC_RAW`, 200,000
single-item samples and 6,250 batches. These are direct scorer costs, not live
kernel lock measurements.

| Route | P50 ns | P95 ns | P99 ns | Max ns |
|---|---:|---:|---:|---:|
| OFF static-branch analogue | 20 | 20 | 20 | 28,631 |
| Generic, 4 features | 20 | 30 | 30 | 104,402 |
| Generic, 6 features | 20 | 30 | 30 | 89,292 |
| Generic, 8 features | 20 | 30 | 40 | 15,730 |
| WPS, 8 features | 20 | 40 | 50 | 97,403 |
| QQ, 8 features | 20 | 30 | 40 | 20,501 |
| FILES, 8 features | 30 | 40 | 40 | 102,812 |
| Generic batch of 128 | 1,840 | 1,960 | 2,060 | 132,583 |

The isolated maxima are scheduler/preemption outliers and are reported rather
than discarded. Native overhead percentage, live `lru_lock` delta, and scan
throughput delta are null/not measured because this kernel was not installed
or booted. Those missing live measurements independently block APPLY.

## Offline replay / SHADOW result

The audit of
`outputs/parp_phase210b_candidate_selector_v2_20260803_224447` returned
`PARP_FRONTIER_SCORE_SHADOW_NOT_SUPPORTED`. It deliberately scored zero rows.

- Formal sessions: WPS 3, FILES 2, QQ 0; required minimum is 4 per App.
- One QQ pilot exists but was not promoted to a formal session.
- Missing candidate fields: `folio_nr_pages`, `frontier_seq`,
  `generation_index`, `native_sort_result`, `nid`, `page_type`, `source_seq`.
- Missing reclaim fields: `frontier_valid_until_ns`, live generation
  capacities, generation efficiency, `nr_reclaimed`, `nr_to_reclaim`.
- `generation_proxy` and `tier_proxy` were found and explicitly rejected as
  real frontier data.
- `score_count=0`, `would_promote_pages=0`, `actual_promote_pages=0`;
  precision/recall/AUC/NDCG are null.
- `trace_lost=0` is only a placeholder: `trace_lost_measured=false` because no
  live kernel trace was collected. It must not be claimed as a zero-loss live
  run.
- No trace proxy was called a real refault; only future live
  `workingset_refault_file/anon` counters can support that claim.

## Risks and unfinished work

1. No genuine post-native-sort frontier dataset or live SHADOW run exists.
2. The model tables are engineering fixtures, not trained/calibrated models.
3. page_ext costs about 48 bytes/base page when enabled.
4. KUnit runtime, live lock latency, scan throughput, trace loss, PSI/OOM gates,
   and real refault effect remain unverified.
5. Only priority-based severe-pressure bypass is implemented in this SHADOW
   version; APPLY additionally requires tested PSI/OOM/no-progress circuit
   breakers.
6. The SHADOW budget accounting marks an epoch selection without behavioral
   promotion; a future APPLY implementation must use atomic claim semantics
   and perform a separately tested exactly-one-generation list/stat update.

Before Phase F, collect at least four formal WPS/FILES/QQ sessions from the
exact post-`sort_folio()` trace schema, validate labels by session and future
window, train/export checksummed App/Generic/Wrong-App models, pass quality and
lock-latency gates, and run KUnit in a compatible VM.

## Future controlled APPLY A/B plan (not executable yet)

The current binary rejects APPLY, so there is no command that can legitimately
enable it. After a new, separately reviewed APPLY build and explicit root,
cgroup-pressure, install/reboot, and APPLY authorization, freeze one experiment
manifest and compare Native, SHADOW, APPLY+Generic, APPLY+correct App, and
APPLY+Wrong-App with the same kernel, VM, target cgroup, saved cgroup limits,
pressure, automation, warm/cold state, duration, and repeated seeds.

The authorized harness must capture before/after
`workingset_refault_file/anon`, pgscan/pgsteal, pgfault/pgmajfault,
`memory.events`, PSI some/full, swap I/O, isolated/reclaimed/promoted pages,
reclaim efficiency, operation P50/P95/P99, CPU, score time, trace loss, and
`lru_lock` latency. Abort on OOM/oom_kill, severe PSI, no reclaim progress,
budget violation, trace loss, stale model/frontier, or latency regression.

Planned control sequence, only after that future authorization:

```sh
# Save first; use the exact target cgroup chosen in the signed manifest.
TARGET_CGROUP=/sys/fs/cgroup/<authorized-target>
for f in memory.high memory.max memory.low; do
    read -r value < "$TARGET_CGROUP/$f"
    printf '%s=%s\n' "$f" "$value"
done

# Per arm: OFF=0, SHADOW=1, future APPLY=2.
printf '%s\n' <authorized-mode> > /sys/kernel/debug/parp/frontier_score_mode
# Run the frozen collector/workload harness, then restore OFF immediately.
printf '0\n' > /sys/kernel/debug/parp/frontier_score_mode
```

Rollback is: write mode 0, stop the authorized workload, restore the three
captured cgroup values exactly, verify no OOM/oom_kill, and reboot to the
previous known-good GRUB entry if the future installed kernel is unstable. No
rollback action is needed now because this work did not change the running
kernel or system controls.

## Phase commits

1. `d3f2137f6` - docs: audit PARP frontier scoring integration
2. `8b64db842` - parp: add quantized reuse score reference model
3. `008871498` - parp: add shadow-only MGLRU frontier scorer
4. `2450d91e4` - parp: test frontier scoring and safety gates
5. `8f6ad4bf3` - parp: add frontier score build and benchmark harness
6. `6749bbd68` - parp: audit real frontier replay support
7. `02f38bfcb` - parp: harden frontier context observability

The timestamped external result directory contains the raw replay audit and
both C/Python benchmark JSON files. The handoff `summary.json` and
`FINAL_REPORT.md` record the final report commit HEAD.
