# 通用 Linux 实验测量系统实施与接口手册

本测量核心按运行时能力发现 `/proc`、cgroup v2 和 tracepoint，不绑定当前 PARP
内核或固定内核版本。正式论文复现实验可以另外冻结目标内核版本。

## 1. 责任边界

### 测试与指标组负责

1. 定义指标名称、单位、方向、数据源和无效条件。
2. 在工作负载启动前完成 before 快照和 eBPF 就绪确认。
3. 在应用/cgroup 退出前完成 after 快照。
4. 保存 raw data、manifest、环境元数据和汇总结果。
5. 判断冷启动/热启动/冷重启、后台存活和实验有效性。
6. 对 Baseline/Candidate 做严格配对统计。

### 自动化组负责

1. 启动或激活目标应用。
2. 完成固定的 QQ/WPS/游戏操作。
3. 确认窗口、首帧或业务 ready 状态。
4. 在动作开始和结束时发布协议事件。
5. 提供帧事件文件或图形采集器输出。

自动化组不需要读取 `/proc/vmstat`、cgroup 或 eBPF map；测量组也不在采集器中实现点击坐标。

## 2. 为什么需要四阶段协议

旧流程在应用写入 start marker 后才拍 before 快照，应用可能已经开始 `exec`、读文件和分配内存，
从而漏掉冷启动最前面的 I/O、CPU、refault 和 Direct Reclaim。新流程固定为：

```text
创建 cgroup
  -> collector 拍 before.json
  -> collector 写 collector-ready.json
  -> workload 等待所有 collector ready
  -> workload 写 workload-start.marker 并立即启动
  -> workload 执行固定场景
  -> workload 写 workload-stop.marker
  -> collector 拍 after.json 和 summary.json
  -> collector 写 collector-done.json
  -> 最后才允许应用/cgroup 退出
```

协议文件均为原子替换写入的 JSON，包含：

```json
{
  "protocol_version": 1,
  "event": "collector_ready",
  "monotonic_ns": 123456789,
  "realtime_ns": 1780000000000000000,
  "pid": 1234
}
```

禁止使用墙钟时间计算延迟；所有窗口计算使用 `monotonic_ns`。`realtime_ns` 只用于与外部日志对齐。

## 3. 自动化接入方式

### 3.1 启动长驻应用

QQ/WPS 等应用由 `memsched_exp.start_marker` 等待多个 collector：

```bash
python3 -m memsched_exp.start_marker \
  --marker RUN/workload-start.marker \
  --ready-file RUN/system/collector-ready.json \
  --ready-file RUN/cgroup/collector-ready.json \
  -- qq
```

该模式写 start marker 后用 `exec` 替换为应用进程。

### 3.2 短任务并保持 cgroup

GB 文件读取等短任务可能在 after 快照前退出。使用 wrapper 模式：

```bash
python3 -m memsched_exp.start_marker \
  --marker RUN/workload-start.marker \
  --ready-file RUN/system/collector-ready.json \
  --ready-file RUN/cgroup/collector-ready.json \
  --stop-marker RUN/workload-stop.marker \
  --done-file RUN/system/collector-done.json \
  --done-file RUN/cgroup/collector-done.json \
  -- command arg1 arg2
```

子命令结束后 wrapper 写 stop marker，并等待 collector done，因此 transient service 的 cgroup 不会提前消失。

### 3.3 仅发布操作边界

```bash
python3 -m memsched_exp.protocol mark --path RUN/operation-start.json --event operation_start
python3 -m memsched_exp.protocol mark --path RUN/operation-stop.json --event operation_stop
```

自动化系统可以添加自己的业务事件文件；必须包含单调时间、应用名、操作名、成功状态和进程身份。

## 4. 采集器命令

```bash
python3 -m memsched_exp.cli collect \
  --name demo \
  --duration 60 \
  --interval 0.5 \
  --cgroup /sys/fs/cgroup/user.slice/.../demo.service \
  --ready-file RUN/collector-ready.json \
  --start-file RUN/workload-start.marker \
  --stop-file RUN/workload-stop.marker \
  --done-file RUN/collector-done.json \
  --manifest-file RUN/manifest.json \
  --output RUN/system
```

`before.json` 必须早于 ready，`after.json` 必须早于 done。输出额外记录：

- `pre_start_boundary_ms`：before 到 start 的边界开销；
- `post_stop_boundary_ms`：stop 到 after 的边界开销；
- `snapshot_elapsed_s`：实际两个快照之间的时间；
- `elapsed_s`：正式 workload 窗口时间，用于 CPU/I/O rate。

计数器差分来自 before/after；边界开销必须保留，不能从原始计数中人工扣除。

## 5. eBPF 规则

`bpf/reclaim.bt` 使用稳定 tracepoint，不依赖易变化的 reclaim kprobe 符号：

- `mm_vmscan_direct_reclaim_begin/end`；
- `mm_vmscan_kswapd_wake/sleep`；
- `oom:mark_victim`。

解析器先对完整流按 TID 配对，再投影到实验窗口。跨越 start/stop 的回收不会造成伪配对失败：

- `direct_reclaim_count`：begin 位于正式窗口内的次数；
- `direct_reclaim_total_duration_ms`：与正式窗口重叠的裁剪持续时间；
- `direct_reclaim_started_full_duration_ms`：窗口内开始事件的完整持续时间；
- `direct_reclaim_boundary_spanning_count`：跨窗口边界的配对数。

collector 缺少 start/stop、全流 begin/end 不配对、JSON 解析错误或 ring buffer 丢事件时整轮无效。

## 6. 冷缓存证明

AppFlow 的目标文件必须经过：

```bash
python3 -m memsched_exp.cache_state evict \
  --path data/appflow-1.2GiB.bin \
  --max-resident-ratio 0.01 \
  --output RUN/cache-eviction.json
```

实现步骤：

1. 用 `mincore` 读取驱逐前驻留页数；
2. 对文件执行 `POSIX_FADV_DONTNEED`；
3. 再次用 `mincore` 检查；
4. 高于阈值则失败。

这只驱逐目标文件，不清空整机所有缓存。`DROP_CACHES=1` 仍只允许在独占测试机使用。

## 7. 进程生命周期指标

PID 可能被复用，不能仅用 `kill -0 PID` 判断热启动。采集：

```bash
python3 -m memsched_exp.process_lifecycle snapshot \
  --pid 1234 --pid 5678 --output RUN/processes-before.json
```

身份由以下字段组成：

```text
boot_id + PID + /proc/PID/stat starttime + cgroup
```

分类规则：

- 原进程不存在，新进程出现：`cold_start`；
- PID、boot_id 和 starttime 全部相同：`hot_resume`；
- PID 相同但 starttime 不同，或切换为新 PID：`cold_restart`；
- 原进程消失：`terminated`。

由此产生冷重启次数、后台存活数和存活率。自动化组只需提供切换前后的目标 PID。

## 8. Manifest 和配对规则

正式运行的 `manifest.json` schema version 为 4，至少包含：

```json
{
  "schema_version": 4,
  "variant": "baseline",
  "scenario": "acclaim-bg8",
  "seed": 20260814,
  "repetition": 1,
  "cache_state": "warm",
  "environment_hash": "...",
  "workload_hash": null,
  "kernel_release": "6.17.x",
  "kernel_commit": "...",
  "policy": {
    "mode": "off",
    "apply_compiled": false,
    "model_provenance": null
  }
}
```

`environment_hash` 包含 CPU、内存、swap/zram、VM sysctl、THP、CPU governor/频率约束、NUMA 和
结果文件系统；不包含被测 kernel/policy 本身。Baseline 和 Candidate 环境指纹不同则拒绝配对。

若 `/sys/kernel/debug/parp` 存在，runner 会自动读取 `effective_tier_mode`、
`effective_tier_stats` 和 `effective_tier_config`，生成 `policy-state.json`，并核对环境变量中的
`POLICY_MODE`、`APPLY_COMPILED`、`MODEL_PROVENANCE`。实际状态不匹配时在工作负载启动前失败，
避免把 Shadow/未编译 Apply 内核误标为 Candidate。自定义路径使用 `POLICY_DEBUGFS_ROOT`。

## 9. 统计输出

`memsched_exp.compare` 对每个指标输出：

- N、mean、median、sample standard deviation；
- p90、p95、min、max；
- Candidate - Baseline 配对差；
- 配对差均值的 bootstrap 95% CI；
- 根据指标方向计算的改善率。

改善率方向：次数、延迟、Jank、CPU、OOM 等越低越好；FPS、吞吐、存活率和缓存容量越高越好。
Baseline 均值为 0 时改善率为 `null`，不能除零或写成 100%。

## 10. 输出目录

```text
RUN/
  manifest.json
  app-metadata.json
  workload-start.marker
  workload-stop.marker
  launch.json
  cache-eviction.json
  reclaim-events.jsonl
  reclaim-events-summary.json
  system/
    metadata.json
    before.json
    samples.jsonl
    after.json
    summary.json
    collector-ready.json
    collector-done.json
  cgroup/
    metadata.json
    before.json
    samples.jsonl
    after.json
    summary.json
    collector-ready.json
    collector-done.json
```

原始文件不得覆盖；失败轮次保留，并通过 `measurement_valid=false` 和 `invalid_reasons` 解释。

## 11. 当前不能自动产生的指标

1. 首个可交互帧：必须由应用、compositor 或图形采集器提供 marker；X11 window 只是代理。
2. 游戏帧：`frames.py` 已实现统计，但帧时间 CSV 必须由外部帧采集器提供。
3. GC working set：必须使用 JVMTI/ART/runtime probe，不由 eBPF 页面事件推断。
4. Java heap ratio：只对 JVM/ART 应用有效，QQ/WPS 不填写。
5. LMKD：Android 专属；Linux 只报告 cgroup/host OOM。

这些字段缺少可信数据源时必须输出 `N/A/null`，不能用 0 填充。

## 12. 验收清单

正式采集前逐项确认：

- Linux release 与本轮 manifest 声明的受控版本一致；未声明固定版本时记录实际 release；
- cgroup v2 和 memory/cpu/io controller 可用；
- before < ready <= start < stop <= after <= done；
- cgroup 首尾路径和 device/inode 相同；
- AppFlow 冷缓存驻留率达到阈值；
- eBPF 全流无 lost/dropped、无配对错误；
- Baseline/Candidate manifest 可严格配对；
- 每场景有效轮次达到预定数量；
- 无升级弹窗、热降频、登录失败和异常网络内容；
- 无效轮次保留原始数据并重新补测。
