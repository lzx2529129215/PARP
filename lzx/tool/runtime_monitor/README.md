# Runtime Monitor v0 使用说明

本目录实现 PC runtime 数据采集器 v0。当前阶段只做本地采集和数据集生成，不做预取、不做 page cache 驱逐、不主动 swap、不修改 Linux 内核，也不做任何内存调度动作。

目标输出：

- `events.csv`：原始文件事件日志。
- `app_events.csv`：应用生命周期与窗口状态事件日志。
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
- 文件事件 fallback：无 eBPF 时通过 `/proc/<pid>/fd` 近似采集 `openat`，通过 `/proc/<pid>/maps` 近似采集 `mmap`。
- 应用级 I/O fallback：通过 `/proc/<pid>/io` 聚合 `read_bytes/write_bytes/rchar/wchar`。
- 应用级内存状态：优先 cgroup v2，失败时 fallback 到 procfs。
- 全局内存状态：采集 `/proc/meminfo` 和 `/proc/vmstat`。
- 前台状态接口：支持 `manual` 和 `x11`；Wayland 当前只保留接口并降级为 manual。
- 应用生命周期事件：通过 procfs 维护 `app_id -> pid_set`，输出 `APP_START/APP_EXIT/APP_CLOSE`。
- 前台切换事件：通过 foreground collector 输出 `APP_SWITCH/APP_FOCUS_IN/APP_FOCUS_OUT`。
- X11 窗口状态事件：尽量采集 `window_id/window_title/pid`，并通过 `_NET_WM_STATE_HIDDEN` 输出 `APP_MINIMIZE/APP_RESTORE`。
- 原生 X11 事件驱动：使用 `--direct-x11-events` 监听 `_NET_ACTIVE_WINDOW`、窗口创建/销毁、`_NET_WM_STATE` 和 Map/Unmap 事件；应用事件不再依赖 `--sample-interval` 轮询。内存、进程等特征仍按采样时钟采集。
- 路径隐私模式：`raw/hash/basename`。
- Ctrl+C 优雅退出并 flush CSV。

Best effort 或未实现：

- path 级 `read/write/fsync/rename` 需要 eBPF/tracefs 才能可靠采集；v0 暂不启用 eBPF。
- `close` 暂未单独输出。
- Wayland 下全局前台窗口受权限限制；v0 默认使用 manual fallback。
- Wayland 下窗口最小化/恢复状态不可可靠获取，当前降级为 manual，不会中断采集。
- `events.csv` 中 `offset/size` 对 fallback 事件只能尽力填充，不能等价于真实 syscall 参数。

已保留但 v0 不默认使用：

- `online_monitor.py`、`predictor.py`、`state.py`、`gnome_extension/` 是前一版在线预测/前台事件对接代码，暂时不删除，后续可接入预测器或 GNOME Wayland 窗口事件。

原生 X11 事件驱动需要安装：

```bash
python3 -m pip install --user -r runtime_monitor/requirements.txt
```

Test2 已默认启用 `--direct-x11-events`。在事件驱动模式下，`direct_app_events.csv` 保存原生事件转换后的 `APP_OPEN/APP_SWITCH/APP_CLOSE/APP_MINIMIZE/APP_RESTORE`，其中 `APP_SWITCH` 和 `APP_OPEN` 直接触发当前 v3 LSTM；最小化、恢复和关闭事件仍会被完整记录并更新打开应用状态。

## 目录结构

```text
runtime_monitor/
  README.md
  monitor.py                  # Runtime Monitor v0 主入口
  online_monitor.py           # 保留：在线预测/D-Bus 对接入口
  collectors/
    foreground.py
    process.py
    file_events.py
    memory.py
    cgroup.py
  core/
    app_mapper.py
    feature_builder.py
    schema.py
    writer.py
  ebpf/
    README.md                 # eBPF 后续扩展说明
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
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --target-app WPS \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --path-mode hash
```

采集 WPS / QQ / Files：

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
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
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
bash runtime_monitor/scripts/run_wps_monitor.sh
```

常用参数：

- `--target-pid <pid>`：只采集指定 PID，并把它归属为目标应用。
- `--target-comm <comm>`：只采集 comm 包含指定字符串的进程。
- `--duration <seconds>`：运行固定时长后退出。
- `--path-mode raw|hash|basename`：控制 `events.csv` 中 path 的隐私模式。
- `--foreground-backend x11|wayland|manual`：前台状态采集后端；Wayland v0 会降级为 manual。
- `--label WPS_LAUNCH|WPS_OPEN_DOC|WPS_SAVE_DOC|IDLE|OTHER`：给本次采集的 `features_1s.csv` 写入统一 label。
- `--enable-ebpf`：预留参数；v0 会打印 warning 并继续使用 procfs fallback。
- `--disable-ebpf`：显式使用 procfs fallback。

## 输出 Schema

### events.csv

字段固定为：

```text
ts_ns,pid,tgid,app,comm,event,path,ext,inode,offset,size
```

v0 fallback 中主要产生：

- `openat`：来自 `/proc/<pid>/fd` 新出现的文件。
- `mmap`：来自 `/proc/<pid>/maps` 新出现的映射文件。

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

当前实现不依赖 eBPF；后续如果接入 eBPF，可以用 `sched_process_exec` 和 `sched_process_exit` 增强启动/退出事件来源。

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
session_id,feature_window_id,window_start_ns,window_end_ns,timestamp,app_id,app_display_name,is_foreground,foreground_duration_ms,pid_count,pids,cgroup_path,comm,exe_path,open_cnt_1s,read_bytes_1s,write_bytes_1s,rchar_1s,wchar_1s,mmap_cnt_1s,fsync_cnt_1s,rename_cnt_1s,unique_inode_cnt_1s,docx_open_cnt_1s,tmp_open_cnt_1s,so_open_cnt_1s,font_open_cnt_1s,pdf_open_cnt_1s,mem_current,anon,file,active_file,inactive_file,pgmajfault_delta,refault_file_delta,closed
```

## WPS 实验流程

### 1. 采集启动 WPS

```bash
cd /home/lzx/Desktop/huawei/huawei_mem/lzx
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

注意：v0 无 eBPF，所以 `fsync/rename` 默认可能一直为 0；后续需要 eBPF/tracefs 才能可靠区分保存路径中的 `fsync/rename`。

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
cd /home/lzxxxxxx/桌面/huawei/myself-kswapd/lzx/tool/runtime_monitor
python3 -m unittest discover -s tests -p 'test_runtime_monitor.py'
```

## 限制

- v0 不做内核修改，不依赖特定 WPS 版本。
- 所有数据只写本地文件，不上传、不外发。
- 如果没有权限读取某些 `/proc` 或 cgroup 文件，对应字段置 0 或空，不让程序崩溃。
- 无 eBPF fallback 可以生成可用趋势特征，但不能替代 syscall 级审计。
