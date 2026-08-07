# Scan-budget observability

`parp_scan_budget_decision` records monotonic timestamp, sequence, scope, domain/app IDs, foreground, score/rank, prediction generation/model/age, native/proposed/applied units, multiplier, pressure, reclaim priority, reason and mode.

`scan_budget_stats` exposes query and scope counts; bind/prior/generation/model failures; foreground/high/medium/low decisions; pressure and clamp events; Observe/Apply counts; native/proposed/applied unit totals; invalid/stale batches; double-scaling rejects; and circuit trips. `scan_budget_circuits` reports each domain's generation, failure count and trip bit; `scan_budget_circuit_clear` clears one domain.
