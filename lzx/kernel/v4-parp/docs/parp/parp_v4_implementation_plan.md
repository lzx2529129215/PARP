# TDD implementation plan

1. Freeze and build upstream.
2. Audit legacy semantics and freeze the contract.
3. Test and implement fixed-point/state primitives.
4. Test and implement file and anonymous scoring.
5. Test and implement immutable snapshots, TTL, and model-version fallback.
6. Add bounded MGLRU observe hooks and prove applied equals original.
7. Run disabled, observe, KUnit, Python, and static matrices.
8. Generate evidence, patch, and safe-stub limitations.
