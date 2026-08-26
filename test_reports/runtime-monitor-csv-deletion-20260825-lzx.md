# Runtime Monitor CSV 清理记录（2026-08-25）

清理范围严格限定为：

`/home/lzx/Desktop/PARP/lzx/service/outputs/runtime_monitor/**/*.csv`

删除前统计：11 个 CSV，共 144,543,016,166 字节（约 134.61 GiB）。根分区使用率 100%，只剩约 124 MiB。

主要文件：

| 字节数 | 最后修改时间 | 路径 |
|---:|---|---|
| 143,802,761,216 | 2026-08-23 19:46:10 | `service_3cc4c78b52a4_20260821_181905/model/global_state_1s.csv` |
| 725,112,082 | 2026-08-23 19:46:10 | `service_3cc4c78b52a4_20260821_181905/model/app_state_1s.csv` |
| 15,021,390 | 2026-08-23 19:46:10 | `service_3cc4c78b52a4_20260821_181905/model/foreground_debug.csv` |
| 97,607 | 2026-08-21 18:40:53 | `service_3cc4c78b52a4_20260821_181905/model/process_events.csv` |
| 22,443 | 2026-08-21 18:40:54 | `service_3cc4c78b52a4_20260821_181905/model/app_lifecycle_events.csv` |
| 386 | 2026-08-21 18:19:05 | `service_3cc4c78b52a4_20260821_181905/model/mglru_markov_debugfs_writes.csv` |
| 373 | 2026-08-21 18:19:05 | `service_3cc4c78b52a4_20260821_181905/model/foreground_events.csv` |
| 235 | 2026-08-21 18:19:05 | `service_3cc4c78b52a4_20260821_181905/model/operation_events.csv` |
| 182 | 2026-08-21 18:19:05 | `service_3cc4c78b52a4_20260821_181905/model/dual_markov_debugfs_writes.csv` |
| 153 | 2026-08-21 18:19:05 | `service_3cc4c78b52a4_20260821_181905/model/operation_labels.csv` |
| 99 | 2026-08-23 19:46:10 | `service_3cc4c78b52a4_20260821_181905/model/lstm_debugfs_writes.csv` |

只删除上述输出树中的 `*.csv`；源码、配置、目录、非 CSV 服务输出、`/home/lzx/Desktop/PARP/test/outputs` 和测试报告均不在删除范围内。

根因证据：`global_state_1s.csv` 占本批 CSV 的 99% 以上，说明当前按秒状态记录存在列/行异常膨胀，不能在修复输出边界前继续启用该常驻服务。

## 清理结果

- 剩余 Runtime Monitor CSV：0 个。
- 保留的非 CSV 输出：24 个。
- 根分区：由 100%（约 124 MiB 可用）恢复为 34%（约 135 GiB 可用）。
- `lsof +L1` 未发现已删除的 Runtime Monitor CSV 仍被进程持有。
- 服务写入进程已经不存在；没有在当前异常内核状态下重启服务，避免再次写满磁盘。
