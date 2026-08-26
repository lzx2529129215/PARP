# 指标字典与通用 Linux 口径

以下字段以 Linux 6.17 参考环境验证，但采集器按字段存在性记录数据源；缺失字段
必须报告为不可用，不能伪造为 0。

## 基本原则

1. 所有计数均用实验窗口末值减初值，原始快照永久保留。
2. 系统值来自 `/proc/vmstat`；前台应用值优先来自独立 cgroup v2 的 `memory.stat`。
3. “次数”“页数”“字节”“毫秒”绝不混用。论文口径无法在通用 Linux 上等价获得时，输出代理指标并标明来源。
4. QQ/WPS 是原生 Linux 应用，不属于 ART Java 应用。LMKD、Java 堆占比、ART GC 工作集和对象重访问率默认 `N/A`。

## 20 项指标

| 指标 | Linux 6.17 实现 | 公式/单位 | 可比性与限制 |
|---|---|---|---|
| Page Re-fault 次数 | `workingset_refault_anon + workingset_refault_file` 的窗口差分 | 页 | 与 Acclaim 的 non-resident refault 概念对齐；内核 shadow entry 的 refault 窗口不是固定“若干秒”。|
| Page Re-fault 比率 | refault 页 / reclaimed 页 | `refault_delta / pgsteal_delta` | 分母使用被回收成功的页，是 Linux 可审计的 evicted-page 代理。分子分母和源字段一并输出。|
| Direct Reclaim 次数 | 首选 eBPF `mm_vmscan_direct_reclaim_begin` 事件数；回退为 `allocstall*` 差分 | 事件 | tracepoint 是进入直接回收的精确事件数；`allocstall` 是分配停顿代理，单独命名。|
| Direct Reclaim 比率 | 主报告采用直接扫描页 /（直接扫描页 + kswapd 扫描页）；补充事件代理为 direct begin /（direct begin + kswapd wake） | 页比例与事件代理 | `direct_reclaim_page_ratio` 是页比例；`direct_reclaim_event_ratio` 是事件代理。两者分开报告，kswapd wake 不等于一次完整后台回收。|
| 应用启动延迟 | `memsched_exp.launch` | ms | 优先 app 写入 `ready-file` 作为首帧标记；X11 `wmctrl` 是首个 mapped window 代理，不证明可交互。Wayland 原生窗口必须用 app 标记或 compositor 插件。|
| 前台应用 Page Re-fault | 前台应用独立 cgroup 的 `memory.stat` | 页 | 必须把完整进程树放入 cgroup；只看主 PID 会漏 Electron/Qt 子进程。|
| 游戏 FPS 及标准差 | 帧时间 CSV，按完整观测窗口的 1 秒桶统计 FPS | FPS | `average_fps`、`fps_per_second_stddev`；完全没有帧的内部秒桶以 0 FPS 计入。可用 `--window-start-ns/--window-end-ns` 固定首尾边界。|
| GB 级应用冷启动延迟 | 无存活进程、清页缓存后，启动到首帧/窗口 | ms | `io_cold_launch.py` 提供 1.2 GiB 合成 I/O 代理；创建时完整写入伪随机数据、`fsync` 并保存 SHA-256 manifest，避免稀疏或未写入 extent。真实应用仍需确保实际读取工作集 >1 GiB。|
| 冷启动次数 | 切回时无原进程/服务 cgroup，产生新进程 | 次 | 需要 round-robin 驱动记录切换前后 PID start time；不能仅凭延迟阈值猜测。|
| I/O 吞吐量 | cgroup `io.stat:rbytes` 差分 / 窗口 | MiB/s | 是实际块层读字节；缓存命中不会计入。合成目标同时输出用户态读取吞吐。|
| 内核直接回收次数 | 与 Direct Reclaim 次数相同 | 事件 | 用户列表中的两个名称语义重复，报告只保存一个规范字段并为展示层建立别名。|
| LMK 事件 | Android 才有 LMKD；通用 Linux 记录 cgroup `memory.events:oom_kill` 和 `oom:mark_victim` | 次 | Linux OOM kill 不是 LMK，结果字段明确为 `oom_kill_count`；QQ/WPS 的 LMK 值为 `N/A`。|
| 后台并发数/存活率 | 结束压力阶段后仍有原 PID start time 的应用数 | `survivors / started` | transient service/cgroup 更可靠；僵尸进程不计存活。|
| 热启动延迟 | 原进程仍活着时，从 activate/切前台到首帧标记 | ms | X11 可用 `wmctrl -a` 加 ready marker；Wayland 需 compositor/app probe。论文 Fleet 使用 20 次、间隔使用其他 app 30 秒。|
| 最大缓存应用数量 | 首次 kill/OOM 前的最大存活后台 app 数 | 个 | Fleet 合成负载为每 app 180 MB；商业应用按 round-robin 两轮。|
| GC 工作集大小 | 修改 ART/JVM runtime 时统计单次 GC 扫描对象数 | 对象/次 | Fleet 原文是 GC 线程访问对象数。通用 eBPF 无法恢复托管堆对象图；QQ/WPS 为 `N/A`。Java 合成负载只提供 app 级代理，不声称是 JVM GC 内部值。|
| 卡顿率 | 超过帧预算的 frame count / total frames | 比率 | 默认 16.7 ms，与 Fleet 60 Hz 口径对齐；高刷新率设备应另报 8.33/11.11 ms。|
| CPU 使用率 | cgroup `cpu.stat:usage_usec` / 墙钟 | % | 同时输出单核等价百分比和整机容量百分比，避免多核下歧义。|
| Java 堆占比 | Java heap used / app total RSS 或 PSS | 比率 | 仅 JVM/ART。Fleet artifact 用 Java heap / app total footprint。QQ/WPS 原生进程不能填此值。|
| 对象重访问比率 | runtime/app probe 标记热启动访问对象 | `distinct_reaccessed / distinct_accessed_in_hot_phase` | 只有 runtime 能可靠判定对象身份。Fleet 合成 Java 负载提供明确定义的代理值；eBPF 页访问不能冒充对象访问。|

## 原始字段与汇总字段

`summary.json` 中：

- `system.page_refault_count`、`system.evicted_pages`、`system.page_refault_ratio`
- `system.direct_reclaim_allocstall_count`
- `system.direct_reclaim_scanned_pages`、`system.kswapd_scanned_pages`、`system.direct_reclaim_page_ratio`
- `cgroup.*` 同口径，并增加 CPU、I/O、OOM 指标
- `sources` 保存本机实际存在且参与计算的内核字段

eBPF 汇总中：

- `direct_reclaim_count`: direct-reclaim begin tracepoint 次数
- `direct_reclaim_total_duration_ms`: begin/end 按 TID 配对后的累计时间
- `direct_reclaim_started_full_duration_ms`: 正式窗口内开始的 reclaim 的完整持续时间
- `direct_reclaim_boundary_spanning_count`: 跨 start/stop 边界的完整配对数量
- `kswapd_wake_count`
- `oom_mark_victim_count`
- `by_comm`: 事件的进程名分布；前台精确归属应结合事件中的 `cgroup_id`

## 有效性规则

- cgroup 两端必须路径和目录 inode 一致，并成功读取 `memory.stat`、`memory.events`、`cpu.stat`、`io.stat`；否则 `cgroup.valid=false`，不会生成伪零指标。
- eBPF 必须各有一个 `collector_start/collector_stop`，direct-reclaim begin/end 数量一致，JSON 无解析错误，stderr 无 lost/dropped events；否则 `valid=false`。
- eBPF 先在完整事件流中配对，再投影到实验窗口；合法的跨窗口事件不会因为窗口内 begin/end 数量不同而误判无效。
- 正式协议必须满足 `before < collector_ready <= workload_start < workload_stop <= after <= collector_done`。
- 严格冷缓存轮次必须保存 `cache-eviction.json`，且 `mincore` 驻留率不高于配置阈值。
- 启动探针只接受启动后新出现的窗口 ID；`start-file` 使用单调时钟标记。
- 汇总 CSV 的 `measurement_valid` 聚合 cgroup、eBPF 和启动探针状态，无效原因写入 `invalid_reasons`，原始文件仍保留。
