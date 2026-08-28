# PARP Runtime Monitor 常驻服务

`parp-runtime-monitor.service` 是 PARP 的常驻用户态服务。它随用户 systemd manager 启动；安装脚本启用 linger，因此无需先登录桌面。服务持续采集 15 个 LSAPP 对齐应用的进程、内存、PageFault、swap 和系统级指标，同时监听 `APP_SWITCH/APP_OPEN/APP_CLOSE/APP_MINIMIZE`，在每个事件上调用 LSAPP-expanded v3 LSTM，并通过一次 `/dev/myfs` ioctl 原子提交“应用概率 + 当前前台标记 + 运行中应用 cgroup 绑定 + 未来工作集”。预测链不再写 `app_prior/app_bind` debugfs。`lzx-note`

原生 X11/GNOME 事件是低延迟主路径；每个采样周期还会用实际活动窗口校对一次前台状态，只补偿原生监听遗漏的切换。补偿事件与迟到的原生事件经过同一状态机按 App/窗口去重，并继续沿 LSTM → `/dev/myfs` 路径提交。`lzx-note`

服务本身不执行 `memory.reclaim`，也不修改 PARP/Tier2/effective-tier 开关；是否采用预测仍由内核编译开关和运行时实验开关决定。`/dev/myfs` 不存在、权限不足或输入不合法时，下沉会 fail-closed，事件监听和指标采集继续运行。

验收场景中的 `fixture-<app>.scope` 保持为独立的可控内存工作集，但在 `runtime_app_scope.service.json` 通过 `binding_scope_names` 映射到对应 GUI 应用的同一 App ID。服务会在一次 `/dev/myfs` 原子更新中同时提交 `automation-<app>.scope` 与 `fixture-<app>.scope` 的不同 domain ID；二者不合并 cgroup，也不共享生命周期。`lzx-note`

服务每秒持续聚合同一 App ID 下 GUI 与 fixture cgroup 的 anon、active file 和有限 inactive file，维护 EWMA 与衰减峰值。事件发生时用 LSTM 概率加权得到未来工作集、当前已驻留部分和预计新增量；只有观测成熟、候选覆盖充分且能唯一定位启用 Tier2 的 policy cgroup 时才设置 ABI v2 的工作集有效位。内核不支持 v2 时自动回退到 ABI v1，不影响原有应用预测下沉。`lzx-note`

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
- `PARP_SERVICE_FOREGROUND_BACKEND`：默认 `desktop`。GNOME Wayland 使用 Shell 扩展的 D-Bus 事件，X11/Xwayland 监听作为回退；服务早于桌面启动时会自动重连。
- `PARP_SERVICE_SCOPE_CONFIG`：默认为跨 slice 的 `runtime_app_scope.service.json`；专项实验可覆盖为单 slice 配置。
- `PARP_SERVICE_APP_VOCAB` / `PARP_SERVICE_GROUP_VOCAB`：默认使用 LSAPP-expanded 15 应用词表。
- `PARP_SERVICE_LSTM_CHECKPOINT`：默认使用 `outputs/lsapp_expanded/checkpoints/app_lstm_switch_v3.pt`。
- `PARP_SERVICE_MYFS_DEVICE`：默认 `/dev/myfs`。
- `PARP_SERVICE_MYFS_MODE`：默认 `apply`；可设为 `dry-run` 只验证编码和事件链。

每个 session 的关键证据：

- `model/direct_app_events.csv`：前台切换、启动、关闭、最小化等事件。
- `model/online_lstm_duration_call_trace.csv`：每次事件进入 LSTM 的调用状态。
- `model/online_lstm_predictions.csv`：v3 应用间概率。
- `parp/myfs_events.csv`：每次原子 ioctl 的 generation、预测数、绑定数及 errno。
- `parp/workingset_predictions.csv`：每次事件的预测工作集、已驻留量、预计增长、置信度、动作提示及逐应用估计。
- `parp/myfs_summary.json`：本 session 的下沉汇总。

安装脚本同时安装并登记 `runtime-app-monitor@huawei.local` GNOME Shell 扩展。首次安装后需要一次注销登录或重启，Wayland 前台切换信号才会开始发送；X11 生命周期回退在此之前仍保持工作。

这些保留策略只处理 `lzx/service/outputs/runtime_monitor` 下名称严格匹配
`service_<boot-id>_<timestamp>` 的直接子目录，不会删除 `test/outputs` 中的验收结果。`lzx-note`
