# 当前 Markov 架构与双模式改造边界

## 当前实现

Runtime Monitor 通过 cgroup workload collector 获取每个应用 scope 的内存 workload delta，分类器生成 `cgroup_workload_state_1s.csv`。现有 Online Causal Markov 链路以应用和 runtime workload 为上下文维护转移，并可将 runtime workload、Markov 表和 LSTM 应用状态写入 MGLRU debugfs。

内核侧当前入口位于 `mm/vmscan.c` 的 `evict_folios()` 扫描批次开始位置。该入口用于 observe-only 统计和 hint 查询，不改变 reclaim、generation、folio isolate 或 `nr_to_scan`。

## 双模式定义

- `CONTINUE=0`：目标应用仍是前台应用时，只使用该应用连续前台 workload 的 `(previous_workload, current_workload) -> next_workload` 历史。
- `REENTRY=1`：目标应用从后台重新成为前台应用时，只使用该应用的 `app_id -> first_workload_after_reentry` 统计；不使用后台期间的 runtime workload 作为查询键。
- `runtime_workload`：collector 的实时 cgroup workload 状态，仅用于运行态观测和独立 debugfs `workload update`。
- `foreground_workload`：仅在应用处于前台时写入的前台历史，和 runtime workload 分开保存。

## 实现边界

本阶段只新增观测、统计、suggestion 和可复核输出。不会启用 LSTM apply，不修改 MGLRU generation、扫描预算、anon/file bias、区域保护、预取、驱逐、swap 或 Tier2 行为；`app_policy_apply=0`、generation adjustment 为 NO_OP。

用户态和内核态均保留旧 `markov set` 兼容接口，但新 `markov continue set` 与 `markov reentry set` 写入独立表，`workload update` 不会覆盖两张新表。

## Debugfs ABI

```text
markov continue set <app_id> <prev> <current> <next> <confidence_fixed> <boost>
markov reentry set <app_id> <next> <confidence_fixed> <boost>
foreground workload <cgroup_id> <app_id> <workload_id> <ttl_ms>
workload update <cgroup_id> <app_id> <workload_id>
```

所有 workload id、app id、confidence 和参数数量在内核解析端校验。clear 命令按 runtime history、foreground history、CONTINUE、REENTRY 独立清除，并保留 `clear all`。
