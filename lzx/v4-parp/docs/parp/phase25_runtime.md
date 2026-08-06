# Phase 2.5 runtime procedure

The userspace bridge is `tools/parp/lstm_prior_bridge.py`. Runtime Monitor should call `AsyncPriorBridge.submit_event()` after its existing LSTM has produced all whitelist scores for one unique lifecycle event ID. The single worker preserves main-loop responsiveness; transport failure changes neither LSTM output nor application state. Open, close, foreground switch, minimize and restore are covered by mock tests.

On a Phase 2.5 kernel, mount debugfs, keep `scan_budget_mode` at `1`, submit one complete batch, write an AppBind with matching model, and read the batch/circuit/stats files. Trigger a controlled target memcg reclaim and capture `parp_scan_budget_decision`; verify applied equals native and global scope is bypassed. Only after reviewing Observe traces should an operator explicitly write `2` for a bounded Apply experiment.

The current host runs Linux 5.15, so live interface and memcg trace validation are NOT_RUN_ENVIRONMENT_GATED. No install, GRUB edit, reboot, or real Apply is part of this phase.
