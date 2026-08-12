# PARP effective-tier integer reference

This package defines the pure-integer contract for the first PARP
effective-tier implementation. It has exactly one `GLOBAL_REUSE_MODEL`, no
App/workload routing, and no generation-frontier inputs. The bundled table is
a deterministic engineering fixture for implementation and parity testing; it
is not trained or calibrated and must not be described as production quality.

The six schema-v1 inputs, in fixed order, are:

1. time since the last real access, in milliseconds;
2. previous real-access interval, in milliseconds;
3. reuse-interval EMA, in milliseconds;
4. consecutive reclaim-candidate count;
5. time in the current generation, in milliseconds;
6. access EMA in Q8.

The score is `bias + sum(weights[feature][bin])`. It is an ordinal reuse rank,
not a probability. An edge belongs to its lower bin. `INT64_MIN` represents a
missing feature and forces an invalid result.
Version, schema, feature-count, missing-state, or score-overflow failures also
force `delta_tier_q8 = 0`, making the ordinary effective decision exactly
Native.

The Q8 policy uses `PARP_TIER_SCALE = 256` and the following fixture values:

```text
score <= -48       -> -1 tier
-48 < score < 48   ->  0 tiers
48 <= score < 96   -> +1 tier
96 <= score < 144  -> +2 tiers
score >= 144       -> +3 tiers only when the explicit cap is 3
```

The normal cap is +2.  The +3 threshold is an engineering-fixture value and
is available only for explicit offline/SHADOW cap-3 ablation; APPLY rejects
cap 3.  A trained artifact leaves hot3 unselected/null unless validation
selects it separately. Downgrade is capped at one tier. The final value is
clamped to `[0, 3 * 256]`, and the comparison is strict:

```text
effective_tier_q8 = clamp(native_tier * 256 + delta_tier_q8, 0, 3 * 256)
effective_protect = effective_tier_q8 > tier_idx * 256
```

The native lazy/workingset special condition remains a separate,
non-overridable protection. Large folios are counted in base pages; scoring
does not multiply their influence by treating one folio as one page. Compressed
timestamps use modulo-2^32 subtraction via `u32_elapsed()`.

## Live SHADOW metadata reservation <!-- #lzx -->

The kernel defaults to OFF without allocating per-page reuse metadata. A live
SHADOW boot must explicitly include `parp_effective_tier_reserve=1` on its
one-time kernel command line. This is intentionally a boot-only reservation:
the page-extension allocator runs before runtime debugfs mode changes, and a
runtime request cannot safely create metadata for pages that are already live.
Without that parameter, `effective_tier_config` reports `metadata_ready 0`
and the SHADOW mode write fails closed with `-EOPNOTSUPP`. The same file
reports `metadata_payload_bytes`; on this host the payload is 24B/page (about
94 MiB at 16 GiB RAM, roughly 125 MiB including page-extension headers). The
read-only live preflight treats missing boot reservation as a blocker and never
changes the mode itself. <!-- #lzx -->

Run from the kernel tree root:

```sh
python3 -m unittest -v tools.parp.effective_tier.tests.test_reference
python3 -m compileall -q tools/parp/effective_tier
```

The test suite builds `cscore.c` with `-Wall -Wextra -Werror` and compares its
GLOBAL score, all three score-to-delta mappings, Q8 clamp/strict comparison,
large-folio accounting, and u32-wrap results against deterministic Python
vectors. The oracle is standalone userspace code and does not modify or boot a
kernel.
