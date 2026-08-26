# 2026-08-21 测试前磁盘空间恢复记录

运行 OOM-THRESHOLD 配对实验前，根分区为 100%，预检无法写出 JSON。只清理以下两份已经停止写入的 runtime-monitor 生成型 CSV，不涉及源码、配置、模型、测试报告或同 session 的其他文件：

- `/home/lzx/Desktop/PARP/lzx/service/outputs/runtime_monitor/service_fff44343c646_20260814_192732/model/global_state_1s.csv`
  - 删除前逻辑/实际大小：97,272,463,360 bytes / 97,272,475,648 bytes
  - 最后修改：2026-08-16 12:23:06 +0800
- `/home/lzx/Desktop/PARP/lzx/service/outputs/runtime_monitor/service_3cc4c78b52a4_20260819_090858/model/global_state_1s.csv`
  - 删除前逻辑/实际大小：54,528,049,152 bytes / 54,528,057,344 bytes
  - 最后修改：2026-08-20 16:01:46 +0800

原因：`duration_history_ms` 等历史字段随服务运行持续增长，导致每秒 CSV 行不断变长。两份文件合计约 151.8 GB，是本次磁盘耗尽的直接来源。

恢复策略：删除上述两个精确生成文件，保留 session 目录、`app_state_1s.csv`、foreground/process/lifecycle 数据及所有测试报告。清理后根分区由 100% 恢复到 33%，可用空间约 138 GB。

## 用户追加清理

用户随后明确要求删除这些天常驻服务生成的全部 CSV。执行前停止 `parp-runtime-monitor.service`，在以下唯一限定目录内删除 `*.csv`：

`/home/lzx/Desktop/PARP/lzx/service/outputs/runtime_monitor`

- 追加删除：139 个 CSV，共 1,186,157,736 bytes；
- 删除后该目录剩余约 260 KiB，保留目录结构和非 CSV 文件；
- 完整删除前清单：[runtime-monitor-csv-deletion-manifest-20260821-lzx.txt](/home/lzx/Desktop/PARP/test_reports/runtime-monitor-csv-deletion-manifest-20260821-lzx.txt)；
- 服务已于 2026-08-21 18:19:05 +0800 使用新 session 重启，状态为 active/running；
- 最终根分区可用空间约 139 GB。
