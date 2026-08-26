# PARP Runtime Monitor 常驻服务

`parp-runtime-monitor.service` 是只观察的 user systemd 服务。它随用户 systemd manager 启动；安装脚本启用 linger，因此无需先登录桌面。服务不写 debugfs、不执行 reclaim、不修改内核开关，只持续采集 15 个 LSAPP 对齐应用的进程、内存、PageFault、swap 和系统级指标。常驻配置不绑定单一 slice，因此能同时观察 `test` 与 `automation` 创建的实验 cgroup。`lzx-note`

安装并立即启动：

```bash
cd /home/lzx/Desktop/PARP
bash lzx/service/runtime_monitor/scripts/install_service.sh
```

状态与日志：

```bash
systemctl --user status parp-runtime-monitor.service
journalctl --user -u parp-runtime-monitor.service -f
```

每次服务启动创建独立目录：

```text
lzx/service/outputs/runtime_monitor/service_<boot-id>_<timestamp>/
```

可在 `~/.config/parp/runtime-monitor.env` 覆盖：

- `PARP_SERVICE_SAMPLE_INTERVAL`：默认 `1.0` 秒。
- `PARP_SERVICE_HISTORY_WINDOW`：默认仅在每行保留最近 `64` 项应用/时长历史，`0` 表示不重复历史；完整切换历史仍保存在事件 CSV。
- `PARP_SERVICE_SESSION_DURATION`：默认 `86400` 秒，服务每日结束当前 session，完成 review 后由 systemd 自动启动新 session。
- `PARP_SERVICE_RETENTION_SESSIONS`：默认最多保留 `7` 个常驻服务 session（含即将启动的 session）。
- `PARP_SERVICE_RETENTION_BYTES`：常驻服务输出默认总配额 `4294967296` 字节（4 GiB）。
- `PARP_SERVICE_MIN_FREE_BYTES`：默认至少给所在文件系统保留 `5368709120` 字节（5 GiB）空闲空间。
- `PARP_SERVICE_STORAGE_CHECK_INTERVAL`：默认每 `60` 秒检查一次输出总量与磁盘空闲线；越界时先完整关闭 CSV、生成 review，再由 systemd 重启并清理旧 session。
- `PARP_SERVICE_FOREGROUND_BACKEND`：默认 `manual`，适合系统启动阶段；桌面 X11 环境可改为 `x11`，此时才启用依赖前台窗口事件的 memory-shadow。
- `PARP_SERVICE_SCOPE_CONFIG`：默认为跨 slice 的 `runtime_app_scope.service.json`；专项实验可覆盖为单 slice 配置。
- `PARP_SERVICE_APP_VOCAB` / `PARP_SERVICE_GROUP_VOCAB`：默认使用 LSAPP-expanded 15 应用词表。

这些保留策略只处理 `lzx/service/outputs/runtime_monitor` 下名称严格匹配
`service_<boot-id>_<timestamp>` 的直接子目录，不会删除 `test/outputs` 中的验收结果。`lzx-note`
