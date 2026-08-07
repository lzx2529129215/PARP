# Scan-budget safety

All invalid inputs fail closed to the native budget. Gates cover explicit mode, unique target scope, active/unexpired AppBind, prior presence/validity, batch generation, model compatibility, TTL, configuration, pressure and per-domain circuit state.

The reclaim path allocates nothing, performs no I/O/Netlink/VMA/rmap traversal and does not sleep. The pure core has no native MM object. Compute occurs once per native budget; apply is an arithmetic-free idempotent read of the decision. No native MM structure or page/folio flag is extended.

Apply is default-off. This phase does not run real Apply, prefetch files, page out anonymous memory, modify generations, install a kernel, alter boot configuration, or reboot.
