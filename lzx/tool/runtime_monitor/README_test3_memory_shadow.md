# Test3：应用间预测的内存价值 SHADOW 验证

Test3 复用 Test2 的原生 X11 事件、在线 v3 LSTM、PARP `app_bind` / `app_prior` 与 snapshot 机制。新增的部分只读取 `/proc/<pid>/smaps_rollup`、应用 cgroup v2 的 `memory.current` / `memory.stat` 以及 automation/X11 时间线。

运行低压力的可重复实验：

```bash
PARP_BRIDGE_MODE=shadow-write \
  runtime_monitor/scripts/run_test3_app_prediction_memory_value.sh --pressure-level low
```

输出位于 `outputs/runtime_monitor/test3_app_prediction_memory_value_<timestamp>/`。`prediction_batches.csv` 是完整预测 batch，`prediction_episodes.csv` 只将下一次真实 `APP_SWITCH` 视为切换结果；`analysis/prediction_memory_value_join.csv` 保留 T0/T1/T2 原始关联和因果状态。

`POTENTIALLY_AVOIDABLE` 需要同时存在：切换前工作集下降、cgroup reclaim/swap 关联计数、切换后工作集重建以及至少一项 App 级 fault/refault/swap-in/近似文件读取证据。它仅为 SHADOW 反事实估算，绝不表示已降低实际延迟。

高压力模式会拒绝运行，直到另行实现并验证不写 cgroup memory 控制、不触发 reclaim 的自然负载验收条件。
