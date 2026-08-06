# Concurrency

Control-plane writers clone and publish immutable snapshots. Readers hold an
RCU read section from prepare to finish. Published snapshots are never changed
in place and old copies are released through `call_rcu`.
