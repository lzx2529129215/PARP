# Region alignment

Worker context resolves PID to task/mm and holds `mmap_read_lock()` while
splitting each aligned half-open DAMON range at VMA and hole boundaries. Every
byte belongs to exactly one classified or unresolved segment. Parsing stops at
64 segments, marks the remainder partial, and records truncated bytes. No raw
VMA pointer survives the worker or enters an RCU snapshot.

