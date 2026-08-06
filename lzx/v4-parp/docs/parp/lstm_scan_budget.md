# LSTM-guided memcg scan budget

Phase 2.5 reimplements the useful application-level intent from legacy v4 without restoring its coupled Markov mechanisms. The existing duration-aware inter-application LSTM emits a complete prediction batch after an application lifecycle event. PARP binds a uniquely targeted memcg domain to an app, validates its prior, and computes native, proposed, and applied MGLRU scan units.

The compiled default is Observe: proposed is visible, applied equals native. Apply exists but requires an explicit privileged mode change and every safety gate. Global, proactive, stale, incompatible, expired, emergency, or circuit-broken contexts remain native.

This controller decides how much a memcg scans. DAMON/PARP region evidence decides which pages are preferred inside the resulting budget. The two controllers are deliberately independent.
