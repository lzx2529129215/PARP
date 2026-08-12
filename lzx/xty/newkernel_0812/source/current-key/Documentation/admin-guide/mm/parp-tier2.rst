.. SPDX-License-Identifier: GPL-2.0

===============================================
PARP per-memcg tier-2 watermarks and prediction
===============================================

PARP can maintain two headroom watermarks for every non-root memory cgroup.
The state is not global: each cgroup owns its configuration, EWMA samples,
arrival estimate, delayed work and counters.  A charge in a nested cgroup
updates that cgroup and each non-root ancestor independently because memory
accounting is hierarchical.

Both the global switch and the cgroup switch default to zero.  Therefore this
facility does not change reclaim until explicitly enabled.

Watermarks and EWMA
===================

For a cgroup with a finite ``memory.max``::

  headroom = memory.max - memory.current
  alloc_wmark = max(PAGE_SIZE, memory.max * alloc_scale / 10000)
  demote_wmark = max(alloc_wmark,
                     memory.max * demote_scale / 10000)

The defaults are 100 (1 percent) and 300 (3 percent), respectively.  Thus the
demote watermark is reached first as headroom falls, while the alloc watermark
is the more urgent level.

The EWMA follows the Huawei v4 design::

  ewma = (previous_ewma * 15 + headroom) / 16

When the EWMA is falling, PARP predicts the time to the demote watermark as::

  delta = previous_ewma - ewma
  predicted_ms = (headroom - demote_wmark) * elapsed_ms / delta

``predicted_ms`` is -1 when headroom is stable/rising, and zero after the
demote watermark has already been reached.  Samples closer than one
millisecond are coalesced so that the rate always has a meaningful timebase.

Scheduling
==========

Prediction work is considered when::

  0 < predicted_ms <= tier2_predict_latency_ms *
                       tier2_predict_horizon_ratio

It is queued after ``max(1, predicted_ms - tier2_predict_latency_ms)``
milliseconds.  The earliest outstanding deadline wins, preventing frequent
charges from continually postponing work.  The work rechecks current
headroom, rejects a stale estimate after recovery, and starts bounded
proactive memcg reclaim.  That reclaim uses the normal MGLRU/PARP path.

Global sysctls
==============

``vm.tier2_predict_enabled``
  Master switch, 0 or 1.  Default 0.

``vm.tier2_predict_latency_ms``
  Expected prediction/reclaim latency, 0 through 1000 ms.  Default 100.

``vm.tier2_predict_horizon_ratio``
  Scheduling horizon multiplier, 0 through 100.  Default 3.

Cgroup v2 files
===============

Each non-root memory cgroup has these files:

``memory.tier2_enabled``
  Per-cgroup switch, 0 or 1.  Default 0.

``memory.tier2_alloc_scale`` and ``memory.tier2_demote_scale``
  Per-cgroup ratios in units of 1/10000, range 0 through 10000.

``memory.tier2_alloc_wmark`` and ``memory.tier2_demote_wmark``
  Current watermarks in bytes.

``memory.tier2_headroom``
  Current finite-limit headroom in bytes.  Zero is reported for an unlimited
  cgroup, for which prediction is disabled.

``memory.tier2_below``
  Current ``alloc`` and ``demote`` threshold states.

``memory.tier2_stats``
  Configuration, live values, EWMA, predicted time and per-cgroup counters.

Example
-------

For ``/sys/fs/cgroup/my-app``::

  echo 1073741824 > /sys/fs/cgroup/my-app/memory.max
  echo 100 > /sys/fs/cgroup/my-app/memory.tier2_alloc_scale
  echo 300 > /sys/fs/cgroup/my-app/memory.tier2_demote_scale
  echo 1 > /sys/fs/cgroup/my-app/memory.tier2_enabled
  sysctl -w vm.tier2_predict_enabled=1
  cat /sys/fs/cgroup/my-app/memory.tier2_stats

Configure each application cgroup separately.  Changing one cgroup's scales
or enabling state does not modify any sibling's watermarks or EWMA history.
