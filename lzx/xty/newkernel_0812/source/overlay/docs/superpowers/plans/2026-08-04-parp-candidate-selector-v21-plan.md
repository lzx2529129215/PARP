# Phase2.10C implementation plan

1. Resolve the two Phase2.10B HEAD identifiers and require a clean canonical source.
2. Create an independent worktree and freeze B outputs, pilot inputs, and all referenced hashes.
3. Reproduce V1 and V2.0 summary metrics before adding v2.1 logic.
4. Implement a causal native-tail comparator, normalized tail distance, T0–T3 bands, and deterministic C1–C4 selection with no-copy partial handling.
5. Add TDD contracts for head resolution, tail ordering, bands, quotas, causality, freeze order, gate precedence, and checkpoint behavior while preserving 116 prior tests.
6. Run development/validation template gates, freeze the conservative template, then evaluate test sessions independently.
7. Produce multi-horizon support, realism, V1/V2/V2.1 comparisons, OOD diagnostics, and Oracle only when gates permit.
8. Recompute before/after hashes, write the eight required handoff tables and final report, and make local commits only.
