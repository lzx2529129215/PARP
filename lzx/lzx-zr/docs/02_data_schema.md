# 数据 Schema

## Observation JSONL

每行是一个窗口：

- `scope_type`：`app|cgroup|region`
- `scope_id`
- `window_start_ns`、`window_end_ns`、`timestamp_ns`
- `sampling_interval_ms`
- `region_ids`、`region_accesses`、可选 `region_timestamps_ns`
- `counters`：WSS、分配、缺页、refault、PSI、direct reclaim、pgscan、pgsteal、swap 和前台等计数/快照

## FeatureVector

输出分为 `scope`、`access`、`reuse`、`hotspot`、`working_set`、`pressure` 和 `data_quality`，所有窗口均保留 scope、时间边界、采样间隔和 feature version。

## Prediction snapshot

快照包含 `current_workload`、`next_workload`、`horizon_ms`、`ttl_ms`、`prediction_seq`、WSS、WSS slope、model version、method、mode 和 native fallback 标志。置信度与概率使用 Q15，最大值 32767。
