# Legacy v4 scan-budget audit

Legacy v4 prepares `mglru_lstm_reclaim_policy` in `mm/vmscan.c:4447`, computes with `mglru_lstm_propose_nr_to_scan()` at 4548, and hooks `try_to_shrink_lruvec()` at 8269/8287. It floors `original * factor / 1000`, saturates to `unsigned long`, replaces zero with a configured minimum, caps increases at original + maximum-extra, and returns original in Observe or proposed in Apply.

Defaults were foreground 700; probability thresholds 9000/5000/2000; high/neutral/low/very-low factors 750/1000/1100/1250; factor bounds 700..1300; missing probability 3000; expired/unknown 1000; minimum one and maximum extra 4096. Rank, anon/file and pressure did not participate.

Retained semantics: foreground/high-prior protection, bounded low-prior increase, TTL, AppBind, Observe/Apply and native fallback. Safely reimplemented: atomic batches/generation/model/rank validation, wide rounded Q15 math, explicit target-scope gating, pressure handling, idempotent apply and per-domain circuit breaking. Not ported: CONTINUE, REENTRY, dual-stage Markov, workload hints, and CURRENT/NEXT suggestion masks. The old hook used each iterated lruvec memcg rather than proving `sc->target_mem_cgroup`, so global traversal could incorrectly receive an app multiplier.
