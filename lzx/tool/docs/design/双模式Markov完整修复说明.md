# 双模式 workload Markov 完整修复说明

## CONTINUE

CONTINUE 只描述同一应用、同一前台 epoch 内的二阶 workload 转移：

```text
(app_id, previous_foreground_workload, current_foreground_workload)
    -> next_workload candidates
```

应用切入前台时递增 `foreground_epoch_id` 并清空窗口；切出时清空窗口并取消 pending prediction。因此切回后的第一个 workload 不会与上一个前台 epoch 拼接。

## REENTRY

REENTRY 描述应用下一次切回前台后的首个有效 workload：

```text
app_id -> first_workload_after_reentry candidates
```

观察窗口接收每个 `status=ok` 的 classifier sample，不要求 `state_changed=true`。首个非 LOW 样本优先；窗口结束且应用仍在前台时允许 LOW fallback；选择前切出则事件无效。每个事件最多产生一个训练样本，样本不能跨事件复用。

## 内核查询

`evict_folios()` 在 isolate 前按 runtime mode 调度 legacy 或 dual hook。dual 前台分支检查 foreground history TTL 后查询 CONTINUE；后台分支先查询 LSTM probability，再按 app 查询 REENTRY。候选按以下顺序稳定选择：

1. `confidence_fixed` 最大；
2. `boost_level` 最大；
3. `rank` 最小；
4. `next_workload_id` 最小。

## reclaim hint 与 suggestion

transition set 只维护模型表，不产生 hint。真实 `continue_hint` / `reentry_hint` 仅由 reclaim prepare 查询生成，记录 target cgroup、上下文、候选数量、selected rank、概率、组合强度和 sequence。

CONTINUE suggestion mask 包括 CURRENT，命中 transition 时再包括 NEXT。REENTRY 始终包括 COMMON，命中 transition 时再包括 WORKLOAD。后台组合强度为：

```text
probability_fixed * confidence_fixed / 10000
```

无 REENTRY transition 但 probability 有效时，只保留 COMMON，强度为 probability。

## runtime mode

- `disabled`：两套 Markov hook 均不执行；
- `legacy`：只执行 legacy；
- `dual`：只执行 dual，默认值；
- `both_observe`：两套都执行，仅供兼容观测。

## 安全边界

本实现保持 observe-only：不修改 `nr_to_scan` 实际值，不修改 folio 或 generation，不实现区域保护，不预取，不主动驱逐，不改 swap，不使用 eBPF/BPF kfunc。Tier2 watermark 是独立链路，本修复不启用 Tier2 runtime。

源码和目标配置构建通过不等于运行态通过。安装新内核前可以把本实现视为 `SAFE_SOURCE_ONLY`，但必须保持 `ready_for_apply=false`。
