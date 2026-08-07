# DAMON integration

Linux 6.17.13 exposes aggregation data in `kdamond_reset_aggregated()` just
before `nr_accesses` is reset. Existing schemes/call APIs cannot subscribe to
arbitrary contexts, and `damon_aggregated` omits PID/mm and interval identity.
Phase 2 therefore uses one conditional call in `mm/damon/core.c`. The adapter
copies scalars and a PID reference, then queues unbound work. DAMON behavior is
unchanged.

`nr_accesses` means positive sampling observations accumulated during a DAMON
aggregation interval. It is not a count of CPU loads/stores.

