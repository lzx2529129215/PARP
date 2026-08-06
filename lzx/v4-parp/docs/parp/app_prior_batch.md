# Application-prior batch

Schema 1 carries `model_version`, strictly increasing `prediction_generation`, monotonic timestamp, horizon, expiry, and up to 32 entries. Each entry carries app ID, score Q15, unique rank, foreground bit, valid bit, and flags.

Userspace builds all whitelist entries and submits them in one write. The kernel rejects duplicate IDs/ranks, invalid range or time metadata, multiple foreground entries, unsupported model/schema, and old/equal generations. It constructs an immutable replacement under the update mutex and performs one RCU publish; reclaim readers can observe only the old complete table or the new complete table.

The debugfs file `app_prior_batch` is the stable controlled transport for this phase. Generic Netlink is still a safety stub and is not represented as live.
