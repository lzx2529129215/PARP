# State prediction

Userspace trains centers, rejection thresholds, and transition tables. Kernel
code assigns the nearest state with UNKNOWN rejection and reads a bounded Q15
transition table keyed by previous/current state, duration, and app-prior bin.
