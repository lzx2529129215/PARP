# PARP Runtime Monitor 常驻服务

`parp-runtime-monitor.service` 是 PARP 的常驻用户态服务。它随用户 systemd manager 启动；安装脚本启用 linger，因此无需先登录桌面。服务通过一个最小化、受 systemd 沙箱限制的 root helper 订阅 Linux proc connector，实时接收全系统进程 leader 的 `FORK/EXEC/EXIT`，主 monitor 本身仍以普通用户运行。它持续采集 15 个 LSAPP 对齐应用的内存、PageFault、swap 和系统级指标，同时监听 `APP_SWITCH/APP_OPEN/APP_CLOSE/APP_MINIMIZE`，在每个事件上调用 LSAPP-expanded v3 LSTM，并通过一次 `/dev/myfs` ioctl 原子提交“应用概率 + 当前前台标记 + 运行中应用 cgroup 绑定 + 未来工作集 + cgroup 工作负载画像”。预测链不再写 `app_prior/app_bind` debugfs。`lzx-note`

全系统进程事件不使用 eBPF，也不修改 tracefs。`parp-process-events@<uid>.service` 只持有订阅 `CN_IDX_PROC` 所需的 `CAP_NET_ADMIN` 和穿越用户私有 runtime 目录、尽力读取进程元数据所需的 `CAP_DAC_READ_SEARCH`，再向 `/run/user/<uid>/parp-process-events.sock` 发送 Unix datagram。socket 位于用户独占的 `0700` runtime 目录；用户服务还通过 `SCM_CREDENTIALS` 强制验证发送者 uid=0。helper instance、连续 `source_seq`、发送丢失、netlink overflow 和心跳超时都会写入独立审计，覆盖缺口不会静默发生。

每一条内核 `PROCESS_START/FORK` 都会进入常驻服务的 `RuntimeMonitorV0.createProcess()`。该入口用当前 `runtime_app_scope.service.json` 的同一套 AppMapper，只对 `prediction_enabled=true` 且已有固定 LSTM `app_id` 的进程提交迁移；未知进程不会创建新 ID。迁移由普通用户服务异步调用 user-systemd `StartTransientUnit(PIDs=[pid])`，目标结构为 `parp-<app>.slice/parp-route-<app>-p<pid>-s<starttime>.scope`。`PROCESS_EXEC` 会按最终 `comm/exe` 再复核一次；启动时及默认每 5 秒还会校对已有进程，覆盖 FORK/EXEC 调度竞争。root connector helper 仍不写 cgroup。

原生 X11/GNOME 事件是低延迟主路径；每个采样周期还会用实际活动窗口校对一次前台状态，只补偿原生监听遗漏的切换。补偿事件与迟到的原生事件经过同一状态机按 App/窗口去重，并继续沿 LSTM → `/dev/myfs` 路径提交。`lzx-note`

服务本身不执行 `memory.reclaim`，也不修改 PARP/Tier2/effective-tier 开关；是否采用预测仍由内核编译开关和运行时实验开关决定。`/dev/myfs` 不存在、权限不足或输入不合法时，下沉会 fail-closed，事件监听和指标采集继续运行。

验收场景中的 `fixture-<app>.scope` 保持为独立的可控内存工作集，但在 `runtime_app_scope.service.json` 通过 `binding_scope_names` 映射到对应 GUI 应用的同一 App ID。服务会在一次 `/dev/myfs` 原子更新中同时提交 `automation-<app>.scope` 与 `fixture-<app>.scope` 的不同 domain ID；二者不合并 cgroup，也不共享生命周期。`lzx-note`

服务每秒持续聚合同一 App ID 下 GUI 与 fixture cgroup 的 anon、active file 和有限 inactive file，维护 EWMA 与衰减峰值。事件发生时用 LSTM 概率加权得到未来工作集、当前已驻留部分和预计新增量；只有观测成熟、候选覆盖充分且能唯一定位启用 Tier2 的 policy cgroup 时才设置工作集有效位。`lzx-note`

在每个 LSTM 事件提交点，服务还读取每个已绑定 cgroup 自身的 `memory.stat`，将它分类为 `ANON_HEAVY`、`FILE_CLEAN`、`FILE_DIRTY` 或 `MIXED`，并给出内核 reclaim 的目标 swappiness（140、40、20、60）和仅对脏文件页启用的受控写回许可。分类不足 8 MiB、置信度不足或绑定不唯一时不下发有效画像；内核会 fail-closed，保留既有策略。支持 ABI v3 的内核会原子接收该画像；旧内核自动回退 ABI v2/v1，不影响原有预测下沉。该采样只发生在预测事件，不生成额外的连续 CSV。`lzx-note`

安装并立即启动：

```bash
cd /home/lzx/Desktop/PARP
bash lzx/service/runtime_monitor/scripts/install_service.sh
```

状态与日志：

```bash
systemctl --user status parp-runtime-monitor.service
journalctl --user -u parp-runtime-monitor.service -f
sudo systemctl status "parp-process-events@$(id -u).service"
sudo journalctl -u "parp-process-events@$(id -u).service" -f
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
- `PARP_SERVICE_ENABLE_MYFS`：默认 `1`。设为 `0` 时服务仍执行桌面事件监听、内存画像和在线 LSTM 推理，但不打开或写入 `/dev/myfs`；仅用于与独立 Linux Native 内核进行同服务开销的配对基线，正常 PARP 启动必须保持 `1`。`lzx-note`
- `PARP_SERVICE_PROCESS_EVENT_SOURCE`：默认 `connector`，实时记录全系统进程 `FORK/EXEC/EXIT`；可设为 `procfs` 恢复只覆盖目标 App 的秒级快照差分。
- `PARP_SERVICE_PROCESS_CONNECTOR_SOCKET`：默认 `/run/user/<uid>/parp-process-events.sock`。
- `PARP_SERVICE_PROCESS_CONNECTOR_READY_TIMEOUT`：默认 `10` 秒；启动期收不到经过 root 凭据认证的 heartbeat 时干净退出并由 systemd 重试。
- `PARP_SERVICE_PROCESS_CONNECTOR_STALE_TIMEOUT`：默认 `10` 秒；运行中事件源心跳超时时结束当前 session，避免把有覆盖缺口的 CSV 伪装成完整采集。
- `PARP_SERVICE_PROCESS_CGROUP_ROUTING`：默认 `systemd`。对已有 LSTM App ID 的新进程执行 user-systemd cgroup 路由；设为 `off` 可只观察、不迁移。
- `PARP_SERVICE_PROCESS_CGROUP_ROUTE_TIMEOUT`：单次 transient-scope 请求超时，默认 `2` 秒。请求在独立 worker 中执行，不阻塞全系统事件接收。
- `PARP_SERVICE_PROCESS_CGROUP_RECONCILE_INTERVAL`：已有进程与竞争窗口的兜底校对周期，默认 `5` 秒。

每个 session 的关键证据：

- `model/direct_app_events.csv`：前台切换、启动、关闭、最小化等事件。
- `model/process_events.csv`：全系统进程 leader 的 `PROCESS_START(FORK)`、`PROCESS_EXEC`、`PROCESS_EXIT`；目标 App 会额外填充 `app`，其他系统进程保留空映射但不会被过滤。
- `model/process_event_source.csv`：root helper 实例、启动/恢复状态、source sequence 缺口、Unix datagram 丢失、netlink overflow 和心跳超时。
- `model/process_cgroup_routes.csv`：每次已有 LSTM App ID 进程的原 cgroup、目标 slice/scope、迁移后 cgroup、状态和耗时；`MIGRATED` 才表示已观察到真实 membership 改变。
- `model/online_lstm_duration_call_trace.csv`：每次事件进入 LSTM 的调用状态。
- `model/online_lstm_predictions.csv`：v3 应用间概率。
- `parp/myfs_events.csv`：每次原子 ioctl 的 generation、预测数、绑定数、工作负载分类、有效画像数及 errno。
- `parp/workingset_predictions.csv`：每次事件的预测工作集、已驻留量、预计增长、置信度、动作提示及逐应用估计。
- `parp/myfs_summary.json`：本 session 的下沉汇总。

安装脚本同时安装并登记 `runtime-app-monitor@huawei.local` GNOME Shell 扩展。首次安装后需要一次注销登录或重启，Wayland 前台切换信号才会开始发送；X11 生命周期回退在此之前仍保持工作。

这些保留策略只处理 `lzx/service/outputs/runtime_monitor` 下名称严格匹配
`service_<boot-id>_<timestamp>` 的直接子目录，不会删除 `test/outputs` 中的验收结果。`lzx-note`
