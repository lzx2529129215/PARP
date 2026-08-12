# PARP Phase2.10C Candidate Selector v2.1 design

Phase2.10C is a pure offline continuation from canonical Phase2.10B HEAD `9fdf54f0d72c...`. It preserves the Phase2.10B causal inactive universe and independent labeler, and changes only the deterministic selector layer.

## Failure boundary

V1 (`GENERATION_TAIL_128_V1`) selects the first 128 rows of the native cold ordering and reproduces zero 60-second positives. V2.0 (`S3/Q_BALANCED`) restores support but violates age and recent-inactive realism gates. Phase2.10C must stay between these boundaries without consuming test labels.

## Tail distance

For each decision, the V1 comparator is applied to the complete legal universe. `tail_rank` is its zero-based rank and `tail_distance = tail_rank / max(|U|-1, 1)`. Rank zero is the coldest/native reclaim priority. Only distance ≤ 0.20 is eligible for a v2.1 main selection.

## Layers and templates

The four mutually exclusive percentile bands are T0 [0,1%], T1 (1,5%], T2 (5,10%], and T3 (10,20%]. C1/C2/C3/C4 quotas are respectively 64/48/16/0, 56/40/24/8, 48/48/32/0, and 40/40/32/16. Every complete decision has 128 unique inactive candidates; shortage is recorded as partial and never padded.

## Causal boundary and labels

Selectors consume only decision-time fields, stable identity tie-breaks, and the V1 comparator. They do not read future rows, labels, operation metadata, session names as scores, paths, names, or content. Candidate rows are written and hashed before the independent 10/30/60/120-second labeler runs; unknown is never negative.

## Selection and gates

Development is WPS/files_01, validation is WPS_02, and test is WPS_03/files_02/QQ pilot. Templates are chosen by legality, validation realism, validation support, then conservative order C1, C3, C2, C4. Test labels never alter the frozen template. Realism requires inactive-only, no distance >20%, T3 ≤12.5%, oldest-half ≥70%, top-10%-tail ≥75%, and selected medians no colder than the universe contract. Support requires 60-second positive ≥20, pairwise ≥10, another horizon positive, and bounded validation ratios. Oracle runs only after all preceding gates.

## Final states

The report distinguishes support-insufficient, realism-gated, oracle-gated, limited, validated, and collection-authorization-required states. A validated state describes offline proxy reconstruction only; it never claims real kernel refault or latency improvement.
