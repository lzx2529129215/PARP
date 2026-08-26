# Runtime Monitor CSV 内容与无限膨胀原因

日期：2026-08-25  
状态：根因已确认，服务当前停止，CSV 已清空；本文只做诊断，尚未修改服务代码。

## CSV 记录什么

### `model/global_state_1s.csv`

每秒一行的全局特征，包括：

- 当前前台应用、前台持续时间、窗口/PID/class/title；
- 观察到、已打开、已关闭、新打开、新关闭的应用；
- 应用切换历史和前台持续时间历史；
- 当前自动化操作、场景、step 和人工标签；
- MemAvailable、major fault、swap in/out、pgscan、pgsteal；
- 测试 slice 路径、`memory.current/high/max`。

### `model/app_state_1s.csv`

每应用每秒一行，包括：

- PID/TGID、comm、exe、cmdline hash、应用 cgroup；
- 是否打开/前台/在测试 slice；
- 每秒 open/read/write/mmap/fsync/rename 和文件类型计数；
- `mem_current`、anon/file/active/inactive file；
- major fault、file refault 和当前自动化标签。

### 事件与调试 CSV

- `foreground_events.csv`：前台应用切换和持续时间。
- `process_events.csv`：进程出现/消失、PID、exe、cgroup。
- `app_lifecycle_events.csv`：应用打开/关闭。
- `operation_events.csv`、`operation_labels.csv`：自动化操作起止、状态和标签。
- `foreground_debug.csv`：xdotool/xprop 的窗口识别过程。
- `mglru_markov_debugfs_writes.csv`、`dual_markov_debugfs_writes.csv`、`lstm_debugfs_writes.csv`：预测/策略向内核 debugfs 写入的命令、状态和错误。
- `review/*.csv`：由 model 数据生成的时间线、应用切换、操作和检查摘要。

## 为什么会按平方膨胀

直接根因是 `duration_history_ms` 和 `app_history` 被设计成“每一行都携带从服务启动至今的完整历史”。

实际调用链：

1. `AppRegistry` 在内存中保存两个无限列表：`app_history` 和 `duration_history`。
2. 前台应用每采样一次，`duration_history` 就追加一个累计前台时长值；采样间隔为 1 秒，因此正常运行时几乎每秒追加一次。
3. `summary()` 每秒用 `"|".join(self.duration_history)` 拼接**全部历史**。
4. `FeatureBuilder` 把这个不断变长的字符串放入 `duration_history_ms`。
5. `CsvWriter` 每秒再把整串历史写入新行并立即 flush。

因此第 1 秒写 1 项，第 10,000 秒写约 10,000 项，第 N 秒写 N 项，总项数为：

`1 + 2 + ... + N = N(N+1)/2`

总磁盘增长是 O(N²)，不是普通按秒日志应有的 O(N)。`app_history` 只在应用切换时追加，膨胀较慢；`duration_history` 每秒追加，是 143.8 GB `global_state_1s.csv` 的主要来源。

删除前的实测比例也支持这一结论：

- `global_state_1s.csv`：143,802,761,216 字节；
- `app_state_1s.csv`：725,112,082 字节；
- 其他 CSV：约 15 MB；
- 服务运行约 49 小时后，global 文件已经占本批 CSV 的 99% 以上。

CSV 列表本身是固定 schema；不是“动态增加列”。问题是固定列 `duration_history_ms` 的单元格无限变长，同时每一行重复之前所有内容。

## 建议的修复顺序

1. **取消逐行完整历史。** `global_state_1s` 只保留本秒值、前一个应用/持续时间、历史计数；完整切换历史从已有 `foreground_events.csv` 重建。
2. **如果模型确实需要窗口，使用有界 deque。** 例如只保留最近 32 或 128 个状态，使每行大小有严格上限，总增长恢复为 O(N)。
3. **添加文件轮转和保留期。** 按 128/256 MiB 或每日轮转，压缩旧文件，只保留最近若干天。
4. **添加硬安全线。** 限制单行字节数、单 session 字节数，并在磁盘剩余空间低于阈值时停止写入和告警。
5. **review 改为流式读取。** 当前 session 结束时会把 CSV 全部读入内存；即使修复平方增长，大型常驻 session 也不应整体加载。
6. **增加长期测试。** 用缩短采样间隔模拟至少 7 天，验证文件大小与样本数线性相关，并验证轮转、重启和低磁盘保护。

在完成至少第 1、3、4 项之前，不建议重新启动常驻服务。它仍为 enabled，机器下次重启会自动启动，需要启动前先完成修复或临时 disable。

## 源码证据

- CSV schema：`/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/core/schema.py`
- 无限历史列表及拼接：`/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/core/app_registry.py`
- 历史写入全局行：`/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/core/feature_builder.py`
- 每行立即写入/flush：`/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/core/writer.py`
- 每秒构造并写入全局行：`/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/monitor.py`

