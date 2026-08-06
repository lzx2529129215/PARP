# Phase 2 limitations

Phase 2 does not train clusters or transition models and never enables Apply.
It does not prefetch, page out anonymous memory, change generations, adjust
real scan budgets, or skip reclaim candidates. Generic Netlink remains limited;
JSONL/CSV export is userspace tooling around safe trace/offline inputs. Domain-
level anonymous cooling is evidence, not a reclaim instruction. Runtime
performance and data-quality conclusions require a later booted test kernel.

