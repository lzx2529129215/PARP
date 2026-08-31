# Runtime Monitor v0 使用说明

> LSAPP 15 应用的 held-out 在线切换场景与运行方法见
> [README_lsapp_expanded.md](README_lsapp_expanded.md)。 <!-- lzx-note -->

本目录实现 PC runtime 数据采集器及常驻预测服务。采集器本身不做预取、不驱逐 page cache、不主动 swap；启用 `--enable-parp-myfs` 时只把 LSTM 上下文下沉给内核，由内核实验开关决定是否参与内存调度。`lzx-note`

文件页工作集的纯影子聚类与下一桶预测见
[`docs/page_hotset_shadow.md`](docs/page_hotset_shadow.md)。该能力复用 eBPF
`page_access`，默认关闭，且不会向内核提交保护、预取或回收动作。

目标输出：

- `model/file_event_source.csv`：eBPF 文件事件源的完整性审计（不保存原始路径）。
- `app_events.csv`：应用生命周期与窗口状态事件日志。
- `model/process_events.csv`：内核实时上报的全系统进程创建、执行与销毁事件。
- `model/process_event_source.csv`：进程事件源的认证、序列缺口、溢出与重启审计。
- `model/process_cgroup_routes.csv`：已有 LSTM App ID 进程的 systemd cgroup 迁移审计。
- `features_1s.csv`：每 1 秒一行的全局窗口、前台应用、应用集合和系统级特征窗口。
- `app_features_1s.csv`：每 1 秒每个 observed app 一行的应用级文件、I/O、内存和进程特征。

当前实验可同时观察 `WPS/QQ/FILES`，后续用于标注和分析：

- `WPS_LAUNCH`：启动 WPS。
- `WPS_OPEN_DOC`：打开文档。
- `WPS_SAVE_DOC`：保存文档。

## 当前能力对齐

已实现：

- 识别 WPS 相关进程：通过 `comm/exe_path` 关键字匹配 `wps/et/wpp/wpspdf/kingsoft/office`，规则在 `configs/runtime/config.yaml` 中配置。
- 进程归属采集：`pid/tgid/comm/exe_path/cgroup_path/start_time`。
- 文件与页缓存事件：独立 root helper 使用 eBPF 捕获 open/mmap/read/pread/write/pwrite/lseek/fsync/access/rename 的进入和返回边沿，并由内核 `readFile/accessFile/evictFile` hook 捕获逐次 read、page-cache access 和真实 eviction；只跟踪 AppProcessIndex 同步的固定 App ID 进程。
- 应用级 I/O fallback：通过 `/proc/<pid>/io` 聚合 `read_bytes/write_bytes/rchar/wchar`。
- 应用级内存状态：优先 cgroup v2，失败时 fallback 到 procfs。
- 全局内存状态：采集 `/proc/meminfo` 和 `/proc/vmstat`。
- 前台状态接口：支持 `desktop/x11/manual`；`desktop` 在 GNOME Wayland 上使用 Shell D-Bus 事件，并保留 X11 回退。`lzx-note`
- 全系统进程生命周期：常驻服务通过受限 root helper 订阅 Linux proc connector，实时输出每个进程 leader 的 `PROCESS_START/PROCESS_EXEC/PROCESS_EXIT`；不受目标 App 过滤。
- 应用进程索引：`createProcess/exeProcess/destroyProcess` 直接维护唯一的 `AppProcessIndex(app_id -> pid_set)`；启动时做一次 `/proc` 基线，事件流缺口时离散重建，正常每秒不再枚举全系统 PID。最后一个 PID 退出后的 grace/关闭兜底也读取该索引。
- 前台切换事件：通过 foreground collector 输出 `APP_SWITCH/APP_FOCUS_IN/APP_FOCUS_OUT`。
- 桌面状态：GNOME 无普通焦点窗口或 X11 活动窗口属于 `gnome-shell` 时映射为 `DESKTOP(app_id=16)`，不再显示 `UNKNOWN`；该运行时状态不改变现有 15-App LSTM checkpoint。
- X11 窗口状态事件：尽量采集 `window_id/window_title/pid`，并通过 `_NET_WM_STATE_HIDDEN` 输出 `APP_MINIMIZE/APP_RESTORE`。
- 原生 X11 事件驱动：使用 `--direct-x11-events` 和常驻 `python-xlib` 连接监听 `_NET_ACTIVE_WINDOW`、窗口创建/销毁、`_NET_WM_STATE` 和 Map/Unmap 事件；窗口属性也在进程内读取，不再周期启动 `xprop/xdotool`。内存、进程等特征仍按采样时钟采集。
- 路径隐私模式：`raw/hash/basename`。
- Ctrl+C 优雅退出并 flush CSV。

Best effort 或未实现：

- `close` 暂未单独输出。
- 非 GNOME 的 Wayland compositor 仍无法可靠提供全局前台窗口；应接入对应 compositor 的受信事件扩展。
- 极长 pathname 在 BPF 端限制为 128 字节；截断会进入事件源完整性元数据。
- eviction 按 `device+inode` 关联到最近访问该文件的已定义 App；共享文件的“最后访问者”归属不能证明唯一因果。block I/O 只精确归属目标 App 线程直接签发的 request，不把异步 writeback 内核线程冒充成某个 App。
- off-CPU 由 `sched_switch` 始终计算；精确 iowait 依赖可选的 `kernel.sched_schedstats=1`，服务不会自行修改该全局 sysctl。

已保留但 v0 不默认使用：

- `online_monitor.py`、`predictor.py`、`state.py`、`gnome_extension/` 是前一版在线预测/前台事件对接代码，暂时不删除，后续可接入预测器或 GNOME Wayland 窗口事件。

原生 X11 事件驱动需要安装：

```bash
python3 -m pip install --user -r runtime_monitor/requirements.txt
```

在事件驱动模式下，`direct_app_events.csv` 保存原生事件转换后的 `APP_OPEN/APP_SWITCH/APP_CLOSE/APP_MINIMIZE/APP_RESTORE`。其中 `APP_SWITCH/APP_OPEN/APP_CLOSE/APP_MINIMIZE` 直接调用当前 v3 LSTM；`APP_RESTORE` 只更新状态，随后的真实前台切换再触发预测。`lzx-note`

## 目录结构

```text
runtime_monitor/
  README.md
  monitor.py                  # Runtime Monitor v0 主入口
  online_monitor.py           # 保留：在线预测/D-Bus 对接入口
  collectors/
    foreground.py
    process.py
    ebpf_file_events.py
    memory.py
    cgroup.py
  core/
    app_mapper.py
    feature_builder.py
    schema.py
    writer.py
  ebpf/
    file_events.bpf.c         # 文件 syscall tracepoint
  helpers/
    ebpf_file_event_helper.py # root BCC loader/转发器
  scripts/
    run_wps_monitor.sh
    label_session.py
    analyze_features.py

configs/runtime/
  config.yaml
  runtime_app_scope.json
  app_mapping.json

outputs/runtime_monitor/
  <session_id>/
    model/
    review/
```

## 运行方式

默认采集 WPS，输出到 `outputs/runtime_monitor`：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --target-app WPS \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --path-mode hash
```

采集 WPS / QQ / Files：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --target-apps WPS,QQ,FILES \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --session-id session_files \
  --path-mode hash
```

也可以使用脚本：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
bash runtime_monitor/scripts/run_wps_monitor.sh
```

常用参数：

- `--target-pid <pid>`：只采集指定 PID，并把它归属为目标应用。
- `--target-comm <comm>`：只采集 comm 包含指定字符串的进程。
- `--duration <seconds>`：运行固定时长后退出。
- `--path-mode raw|hash|basename`：控制 `events.csv` 中 path 的隐私模式。
- `--foreground-backend desktop|x11|wayland|manual`：常驻服务默认 `desktop`，组合 GNOME D-Bus 与 X11 回退。
- `--label WPS_LAUNCH|WPS_OPEN_DOC|WPS_SAVE_DOC|IDLE|OTHER`：给本次采集的 `features_1s.csv` 写入统一 label。
- `--process-event-source connector`：通过受限 root helper + Linux proc connector 实时采集全系统进程 leader 的 `FORK/EXEC/EXIT`。
- `--process-event-source procfs`：旧兼容模式；没有全系统实时完整性保证。常驻生产服务必须使用 `connector`，才能由三个 handler 持续维护 `AppProcessIndex`。
- `--require-process-connector`：认证事件源未就绪或心跳超时时干净结束 session，避免静默产生不完整覆盖。
- `--process-cgroup-routing systemd`：每个 `PROCESS_START/PROCESS_EXEC` 都分别调用常驻服务 `createProcess()/exeProcess()`；仅将 runtime scope 中已有 LSTM App ID 的进程迁入 `parp-<app>.slice`。
- `binding_scope_names`：按完整 cgroup 组件把通用 fixture 可执行文件映射到固定 App ID；索引保存独立 `fixture` 角色，迁移但不参与 GUI 生命周期。
- 即时 journal 日志只输出与上述固定 App ID 有关的 `createProcess/exeProcess/destroyProcess`；未知进程仍由 connector 捕获并写入全量 `process_events.csv`，但不打印 `EVENT_TRIGGER`。
- `--file-event-source ebpf --require-ebpf-file-events`：启用严格 eBPF 文件 syscall 采集；不提供 fd/maps fallback。
- `--enable-page-hotset-shadow`：启用按前台应用聚合的文件页热集聚类和下一桶影子预测；要求 `--file-event-source ebpf`。
- `--enable-ebpf`：兼容旧命令的 deprecated alias；现在选择 proc connector，但不会加载 eBPF 程序。
- `--disable-ebpf`：旧进程源兼容参数，与新的 `--file-event-source` 无关；生产常驻服务不使用。

## 输出 Schema

### model/file_event_source.csv

该文件只保存 eBPF 来源健康状态和丢失计数。逐 syscall/page 事件与原始 path
不长期落盘；`app_state_1s.csv` 保存按 App 聚合后的请求/返回字节、延迟、offset
访问模式、page access/eviction、page fault、block I/O 和 off-CPU/iowait。

### app_events.csv

字段固定为：

```text
ts_ns,event_type,app,pid,tgid,window_id,window_title,old_app,new_app,foreground_app,duration_ms,source
```

事件类型：

- `APP_START`：应用相关进程首次出现，来源 `procfs`。
- `APP_EXIT`：应用相关进程退出，来源 `procfs`。
- `APP_CLOSE`：某 `app_id` 对应的所有进程都退出。WPS 这类多进程应用只有最后一个相关进程退出时才会输出。
- `APP_SWITCH`：前台应用从 `old_app` 切换到 `new_app`。
- `APP_FOCUS_IN`：应用获得前台焦点。
- `APP_FOCUS_OUT`：应用失去前台焦点，`duration_ms` 表示上一次前台持续时间。
- `APP_MINIMIZE`：X11 窗口进入 `_NET_WM_STATE_HIDDEN`。
- `APP_RESTORE`：X11 窗口从 `_NET_WM_STATE_HIDDEN` 恢复。

常驻生产服务默认使用 proc connector 实时获取全系统 `FORK/EXEC/EXIT`。三种事件是 `AppProcessIndex` 的 steady-state 唯一写入来源；`/proc` 全 PID 枚举只在启动基线或已审计的事件流缺口恢复时执行，不再是每秒任务。文件 syscall 则由独立 eBPF helper 捕获，两条内核事件源各自做凭据、序号、丢失和心跳审计。

### model/process_events.csv

常驻服务使用 `connector` 时，一条内核事件写一行：

```text
session_id,ts_ns,timestamp,event_type,app,pid,tgid,comm,cmdline_hash,exe_path,cgroup_unit,cgroup_path,test_slice,in_test_slice,boot_ts_ns,native_event,parent_pid,parent_tgid,exit_code,exit_signal,cpu,source_seq,source_instance_id,source
```

- `PROCESS_START` 对应内核 `FORK`，包含父进程 PID/TGID。
- `PROCESS_EXEC` 对应 `EXEC`，用于取得 exec 后的程序名、路径和 cgroup 元数据。
- `PROCESS_EXIT` 对应 `EXIT`，包含退出码和退出信号。
- 这里只按 `pid == tgid` 保留进程 leader，不把同一进程的每个线程重复算作新进程。
- 所有进程都会落盘；只有能命中运行时映射规则的行才填写 `app`。

### model/process_event_source.csv

该文件是“是否真的覆盖完整”的独立证据。`SOURCE_RESTART`、`DELIVERY_GAP`、
`KERNEL_OVERFLOW`、`ERROR` 或 `SOURCE_STALE` 都表示当前 session 不能视为无缺口；
生产服务的严格模式会结束当前 session，再由 systemd 重启。

### model/process_cgroup_routes.csv

该文件记录事件驱动的 cgroup 决策。`PROCESS_START` 对每个新进程调用
`RuntimeMonitorV0.createProcess()`，`PROCESS_EXEC` 调用 `exeProcess()`；未命中当前 LSTM runtime scope 固定 App ID 的
进程原地保留。命中后由异步 worker 请求 user-systemd 创建 transient scope，
并重新读取 `/proc/<pid>/cgroup`；只有真实路径进入目标 `parp-<app>.slice` 才写
`MIGRATED`。`exeProcess()` 会复核最终可执行文件，同时新增、刷新、移动或移除
`AppProcessIndex` 中的 PID；`RECONCILE` 只是低频 cgroup 路由完整性兜底。

### features_1s.csv

字段至少包含：

```text
session_id
feature_window_id
window_start_ns
window_end_ns
timestamp
foreground_app
foreground_duration
window_title
observed_apps
open_apps
closed_apps
app_history
duration_history
global_mem_available
global_pgmajfault_delta
global_pswpin_delta
global_pswpout_delta
global_pgscan_delta
global_pgsteal_delta
manual_label
```

`features_1s.csv` 不再包含 `wps_*` 这类应用强相关字段。WPS / QQ / FILES 的应用级特征统一写入 `app_features_1s.csv`。

### app_features_1s.csv

字段至少包含：

```text
session_id,feature_window_id,...,app_id,...,read_ops_1s,write_ops_1s,read_requested_bytes_1s,read_bytes_1s,read_latency_ns_p95_1s,lseek_cnt_1s,sequential_read_ops_1s,cyclic_read_ops_1s,random_read_ops_1s,read_access_pattern,page_access_cnt_1s,eviction_cnt_1s,user_page_fault_cnt_1s,attributed_block_io_bytes_1s,offcpu_sleep_ns_1s,offcpu_blocked_ns_1s,iowait_ns_1s,...
```

## WPS 实验流程

### 1. 采集启动 WPS

```bash
cd /home/lzx/Desktop/PARP/lzx/service
python3 runtime_monitor/monitor.py --output-dir outputs/runtime_monitor/wps_launch --label WPS_LAUNCH --path-mode hash
```

另一个终端启动 WPS，等待几秒后回到 monitor 终端按 `Ctrl+C`。

### 2. 采集打开 docx

```bash
python3 runtime_monitor/monitor.py --output-dir outputs/runtime_monitor/wps_open_doc --label WPS_OPEN_DOC --path-mode hash
```

在 WPS 中打开一个 `.docx` 文档，等待几秒后按 `Ctrl+C`。

### 3. 采集保存文档

```bash
python3 runtime_monitor/monitor.py --output-dir outputs/runtime_monitor/wps_save_doc --label WPS_SAVE_DOC --path-mode hash
```

编辑文档并保存，等待几秒后按 `Ctrl+C`。

### 4. 查看结果

```bash
head -20 outputs/runtime_monitor/wps_open_doc/events.csv
head -20 outputs/runtime_monitor/wps_open_doc/app_events.csv
head -20 outputs/runtime_monitor/wps_open_doc/features_1s.csv
python3 scripts/analyze_features.py outputs/runtime_monitor/wps_open_doc/features_1s.csv
```

重点观察：

- 打开文档：`wps_docx_open_cnt_1s`、`wps_mmap_cnt_1s`、`wps_read_bytes_1s` 是否上升。
- 保存文档：`wps_write_bytes_1s`、`wps_tmp_open_cnt_1s` 是否上升。
- 全局状态：`global_mem_available`、`global_pgmajfault_delta`、`global_pswpin_delta`、`global_pswpout_delta` 是否每秒有记录。

生产服务启用 eBPF 后，`fsync/rename` 来自真实 syscall 返回边沿（包括可审计的失败返回）；若关闭文件事件源，这些字段为 0，服务不会用轮询数据伪装。

## 手动标注

运行时可直接使用 `--label` 写入整段采集的标签：

```bash
python3 runtime_monitor/monitor.py --output-dir outputs/runtime_monitor/session1 --label WPS_OPEN_DOC
```

也可以采集后修改：

```bash
python3 runtime_monitor/scripts/label_session.py \
  outputs/runtime_monitor/session1/features_1s.csv \
  outputs/runtime_monitor/session1/features_1s.labeled.csv \
  WPS_OPEN_DOC
```

## 在线预测对接代码

暂时保留以下文件，当前 v0 不默认调用：

- `online_monitor.py`：原 GNOME D-Bus 窗口事件 + 在线预测入口。
- `predictor.py`：LSTM 在线预测适配层。
- `gnome_extension/`：GNOME Shell 扩展。
- `configs/runtime/app_mapping.json`：窗口应用名到预测器词表的映射。

后续如果要把 v0 的 `features_1s.csv` 或前台事件接入预测器，可以基于这些文件继续对接，不需要重新实现预测加载逻辑。

## 测试

```bash
cd /home/lzx/Desktop/PARP/lzx/service/runtime_monitor
python3 -m unittest discover -s tests -p 'test_runtime_monitor.py'
```

## 限制

- v0 不做内核修改，不依赖特定 WPS 版本。
- 所有数据只写本地文件，不上传、不外发。
- 如果没有权限读取某些 `/proc` 或 cgroup 文件，对应字段置 0 或空，不让程序崩溃。
- eBPF 被关闭或不健康时没有文件事件 fallback；严格常驻配置会结束 session，避免把缺口数据标为完整。
