# v4.1-PARP 架构与边界

## 基础继承

v4.1 继承 v4-parp 的四条约束：

1. 用户态准备模型和控制快照，内核热路径只做有界查询/评分。
2. `app_bind` 将运行中的 domain/cgroup 映射到 App ID。
3. `app_prior` 携带 App 的 next-use 先验、rank、horizon、TTL 和 model version。
4. 控制失效、证据缺失或版本不一致时回退 Native。

## LSTM 的位置

应用间 LSTM 读取历史前台 App、打开 App 集合、时间特征和用户组，输出未来 horizon 的 App 分数。v4.1 对当前前台 App 做候选过滤，然后把候选分数作为 App 级先验：

```text
LSTM score(a)
  -> App candidate score
  -> expected launch working set
  -> target headroom
  -> total reclaim target
  -> per-App reclaim budget
```

页面级 PARP 评分仍属于 v4-parp；v4.1 不在页面模型中加入 `app_id`，也不在 reclaim 热路径调用 LSTM。

## 两个对照策略

- Native：只保留当前前台 App 的保护权重，不使用 next-App 预测。
- LSTM counterfactual：在相同样本、相同可回收页规模和相同可用内存下，额外使用 next-App score。

主要判断指标为：

- `hit_at_1`、`hit_at_k`、`mrr`：预测是否命中真实下一 App；
- `headroom_abs_error`：预测启动工作集与真实下一 App 启动工作集的误差；
- `actual_next_budget_reduction_pages`：下一 App 若已运行，LSTM 相对 Native 少承担多少回收预算；该值允许为负，负值表示“为其他未运行候选预留 headroom”带来了总预算 trade-off；
- `lstm_changed_reclaim_target_samples`：预测是否改变了总回收目标。

## 当前不做的事

不启用 Apply，不直接写 `/sys/kernel/debug/parp/*`，不修改 generation、scan budget、anon/file 选择、swap、prefetch 或页面生命周期。`emit_parp_commands.py` 只生成审计文件。
