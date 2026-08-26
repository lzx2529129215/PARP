# Runtime Monitor CSV 膨胀修复与验证（2026-08-26）

## 结论

已修复 `global_state_1s.csv` 因重复写入完整生命周期历史而呈 O(N²) 膨胀的问题，并为常驻服务增加会话轮换、数量/容量保留和运行中磁盘保护。`test/outputs` 不在自动清理范围内。

## 修改

1. `AppRegistry` 的 `app_history` 与 `duration_history` 改为 `deque(maxlen=64)`；可通过 `--history-window` 或 `PARP_SERVICE_HISTORY_WINDOW` 调整，设为 `0` 可禁用重复历史列。完整前台切换仍由事件 CSV 保存。
2. 常驻服务默认每 86400 秒优雅结束一个 session，由 systemd 自动启动新 session。
3. 默认最多保留 7 个服务 session，总配额 4 GiB，并保留至少 5 GiB 文件系统空闲空间。
4. 每 60 秒运行一次存储守卫；达到容量/空闲线时先关闭 CSV、生成 review，再轮换并清理旧 session。
5. 清理器只接受输出根目录的直接子目录，且目录名必须严格匹配 `service_<12位boot-id>_<日期>_<时间>`；拒绝符号链接和越界路径。

## 验证

- Python 编译检查：通过。
- Shell 语法检查：通过。
- Runtime Monitor 测试：102 项通过，3 项因环境能力跳过。
- 真实服务超过 64 个采样点后，连续 5 行的 `duration_history_ms` 项数均为 64，不再随服务生命周期增加。
- 修复后真实服务第 87 个采样附近：`global_state_1s.csv` 约 52 KiB，末尾连续 5 行均约 708 字节。
- 最终常驻进程参数已包含：`--history-window 64`、`--duration 86400`、`--max-output-root-bytes 4294967296`、`--min-output-free-bytes 5368709120`、`--storage-check-interval 60`。
- 服务状态：`active`、`enabled`，重启次数 0。
- 输出根目录当前 7 个服务 session；启动保留策略时清理了 7 个旧且不含 CSV 的服务 session 目录。
- 当前磁盘约 136 GiB 可用，占用率 34%。

## 默认配置

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `PARP_SERVICE_HISTORY_WINDOW` | 64 | 每行最多重复的短期历史项数 |
| `PARP_SERVICE_SESSION_DURATION` | 86400 秒 | 每日轮换 session |
| `PARP_SERVICE_RETENTION_SESSIONS` | 7 | 服务 session 数量上限 |
| `PARP_SERVICE_RETENTION_BYTES` | 4294967296 | 服务输出总配额 4 GiB |
| `PARP_SERVICE_MIN_FREE_BYTES` | 5368709120 | 文件系统空闲保护线 5 GiB |
| `PARP_SERVICE_STORAGE_CHECK_INTERVAL` | 60 秒 | 运行中存储检查周期 |

`lzx-note`
