# 双模式 Hint 计数语义

CONTINUE 与 REENTRY 分别输出四类计数：

1. `lookup_calls`：进入对应候选查询的次数。
2. `lookup_hits`：找到有效候选的次数。
3. `hint_generation_events`：有效候选形成 observe-only hint 的次数。
4. `hint_state_updates`：新 hint 状态与该 app/cgroup 上一条状态不同的次数。

状态比较使用 mode、app_id、previous_workload、current_workload、predicted_workload、
confidence_fixed、boost_level、suggestion_mask、lstm_probability_fixed 和
combined_strength_fixed。重复命中同一状态时只刷新 `last_seen` 并增加
`repeated_hit_count`，不重新覆盖完整 hint，也不增加 `hint_state_updates`。

REENTRY 的组合强度使用 `((u64) probability * confidence + 5000) / 10000`，即以
万分比进行四舍五入计算。debugfs hint 同时输出输入概率、REENTRY 置信度、组合强度
和 `combined_formula_valid=1`，便于用户态审计。

这些统计和 hint 都是 observe-only。它们不改变 `nr_to_scan`、folio generation、
isolation、aging 或 reclaim 结果。
