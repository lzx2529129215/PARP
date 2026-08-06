# Safety

Observe mode always applies the native action. Hot hooks allocate no memory,
perform no I/O, send no messages, train no model, and perform no unbounded
VMA/rmap traversal. Missing or stale evidence returns NATIVE.
