# App Bind 表修复说明

## 问题

Bindfix 前，`app bind <app_id> <cgroup_id> <ttl_ms>` 仅按 `cgroup_id`
查找已有槽位。若找不到相同 cgroup，它只会选择空槽位；已经过期的槽位
仍保持 `valid=1`，因此既不作为空槽位使用，也不会被替换。

Runtime Monitor 在启动、每个刷新周期和前台应用变化时都会写入绑定。新的
automation session 会产生新的 cgroup inode ID；旧 session 的 TTL 到期后仍占据
表项。表满后，对新 cgroup 的写入返回 `-ENOSPC`，后续对已存在 cgroup 的刷新
仍可成功，造成同一轮中部分应用持续失败的现象。

## 修复语义

容量保持 32，不扩大数组。固定槽位在持有 `mglru_workload_markov_lock` 时按以下
顺序 upsert：

1. 相同 `(app_id, cgroup_id)`：刷新 TTL 和时间戳。
2. 相同 `cgroup_id`：替换 app ID，并刷新 TTL。
3. 相同 `app_id`：替换 cgroup ID，并刷新 TTL。
4. 已过期槽位：清零后复用。
5. 空槽位：插入。
6. 仅当所有槽位都是有效且唯一的绑定时返回 `-ENOSPC`。

过期判断沿用 `time_after()`，因此具备 wrap-safe jiffies 语义。没有动态内存
分配，也不在 reclaim 热路径写表。

## 新 ABI 与统计

`clear bind` 和兼容别名 `clear app_bindings` 仅清空绑定表；它们不会清空
probability、runtime history、foreground history、CONTINUE、REENTRY、hint 或 policy。
`clear all` 仍会包含绑定清理。

新增 `app_bind_*` 统计用于区分 insert、refresh、替换、过期复用与真正 ENOSPC。
它们仅用于 debugfs 观察，不影响 `nr_to_scan`、folio generation 或 reclaim 行为。
