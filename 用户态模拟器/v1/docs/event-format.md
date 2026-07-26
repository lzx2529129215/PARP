# Event format

Each non-empty, non-comment line contains one command and whitespace-separated arguments. `#` starts a comment. Parsing is strict: an invalid line reports filename, line number, original text and a reason; the event is not applied and replay stops.

## Commands

```text
GROUP_CREATE <cgroup_id>
GROUP_DESTROY <cgroup_id>
GROUP_SET_SWAPPINESS <cgroup_id> <0..200|INHERIT>
GROUP_SET_SWAP_ENABLED <cgroup_id> <0|1|INHERIT>
GROUP_SET_SWAP <cgroup_id> <ON|OFF|INHERIT>       # compatibility alias
PAGE_ADD <page_id> <cgroup_id> <ANON|FILE> <order 0..63>
PAGE_ACCESS <page_id> <access_cgroup_id>
PAGE_REMOVE <page_id>
PAGE_RECHARGE <page_id> <new_cgroup_id>
PAGE_MIGRATE <old_page_id> <new_page_id>
PAGE_EXEC_OUTCOME <page_id> <SUCCESS|PUTBACK|ACTIVATE|BUSY|DIRTY|WRITEBACK|UNEVICTABLE>
AGE_GROUP <cgroup_id>
AGE_ALL
RECLAIM_GROUP <cgroup_id> <target_base_pages>
RECLAIM_ALL <target_base_pages>
VALIDATE
DUMP
```

Scenario assertions are:

```text
ASSERT_PAGE_MISSING <page_id>
ASSERT_PAGE_STATE <page_id> <NEW|ON_LRU|ISOLATED|UNEVICTABLE>
ASSERT_PAGE_LRU <page_id> <NONE|INACTIVE_ANON|ACTIVE_ANON|INACTIVE_FILE|ACTIVE_FILE>
ASSERT_DOMAIN_PAGES <cgroup_id> <base_pages>
ASSERT_LAST_STOP_REASON <TARGET_REACHED|NO_SCANNABLE_PAGES|NO_PROGRESS|PRIORITY_EXHAUSTED|EXECUTOR_ERROR|ROUND_LIMIT>
```

`GROUP_SET_SWAP` is accepted as a compatibility alias for the design document spelling. `PAGE_RECHARGE` requires a non-isolated page and changes Shadow owner/list; cross-cgroup access does not. `PAGE_REMOVE` rejects isolated pages. Destroying a nonempty domain is an error.

## Complete example

```text
GROUP_CREATE 10
GROUP_CREATE 20
PAGE_ADD 100 10 ANON 0
PAGE_ADD 200 10 FILE 1
PAGE_ACCESS 100 20
AGE_GROUP 10
PAGE_EXEC_OUTCOME 200 PUTBACK
RECLAIM_GROUP 10 1
ASSERT_PAGE_STATE 200 ON_LRU
DUMP
VALIDATE
```
