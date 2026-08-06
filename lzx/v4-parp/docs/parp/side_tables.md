# Side tables

Mutable builders run only in asynchronous worker context and grow in bounded
chunks. Defaults cap file regions per domain at 4096, anonymous regions per
domain at 2048, and total regions at 65536. Immutable, sorted snapshots contain
only neutral scalars and are RCU-published with a maximum 60-second TTL.
File-folio lookup is binary-search based. Anonymous evidence is published as a
domain-level cooling summary, avoiding per-folio rmap/VMA work.

