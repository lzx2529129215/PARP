# Runtime Monitor Region Monitor

本模块是 observe-only 的 cgroup + DAMON region 采集链路。它只读取用户态可见的 cgroup、`/proc/<pid>/maps`、DAMON sysfs/tracefs 信息，不写 `lru_gen_pages`，不启用 Tier2，不执行预取、主动驱逐、swap 修改或 MGLRU Apply 行为。

## 架构

`monitor.py` 在显式传入 `--enable-region-monitor` 后启动 sidecar：

```bash
python3 -m runtime_monitor.region_monitor.region_monitor \
  --session-dir outputs/runtime_monitor/<session> \
  --config runtime_monitor/config/region_monitor.json \
  --app-scope-config configs/runtime/runtime_app_scope.json
```

采集流程：

1. `capability_probe.py` 检查内核配置、DAMON sysfs、tracefs、tracepoint 和权限。
2. `cgroup_pid_tracker.py` 从目标 app scope 的 `cgroup.procs` 动态发现 PID，并用 `/proc/<pid>/stat` 的 `starttime` 防止 PID 复用。
3. `process_role_resolver.py` 用配置化规则把进程归类为 `WPS_MAIN`、`WPS_CLOUD_SERVICE`、`WPS_LIBADAPTER`、`WPS_OTHER` 或 `UNKNOWN`。
4. `vma_parser.py` 只读取 `/proc/<pid>/maps`，不高频读取 `smaps`。
5. `tracefs_event_source.py` 读取 `damon:damon_aggregated` tracepoint format，并按实际字段解析事件。
6. `window_aggregator.py` 将 DAMON 动态虚拟地址区间映射到稳定 region key，并输出稀疏窗口。

## 为什么不使用绝对虚拟地址

PID、PFN、绝对虚拟地址和 VMA 序号都会随进程生命周期、ASLR、文件加载顺序和重启变化。长期 region 身份不能依赖这些值。当前实现只把它们用于单次运行内解析，不写入稳定 key。

## 文件 region key

role-aware key：

```text
app_id + process_role + dev_major + dev_minor + inode + file_offset_bucket + permissions
```

canonical file key：

```text
dev_major + dev_minor + inode + file_offset_bucket
```

`file_offset_bucket = floor((vma.file_offset + virtual_address - vma.start_addr) / region_bucket_bytes)`，默认 bucket 为 256 KiB。

## 匿名 region key

匿名 region 第一版使用粗粒度语义身份：

```text
app_id + process_role + anon_type + anon_name + permissions + vma_size_bucket + relative_offset_bucket
```

匿名 region 的 `identity_confidence` 分为：

- `HIGH`：具名匿名映射；
- `MEDIUM`：heap、stack、graphics 等可辨认类型；
- `LOW`：未知匿名映射。

匿名 region 不能保证跨应用重启完全稳定，只作为操作识别辅助特征，不作为第一版真实页面保护依据。

## DAMON 说明

DAMON 是采样和聚合机制，不是精确逐次访问日志。过大的 DAMON region 会被标记为 `LOW_RESOLUTION`，不会伪装成页级精度。

## 输出文件

所有输出写到当前 Runtime Monitor session 下：

```text
region_monitor/capability_report.json
region_monitor/capability_report.md
region_monitor/region_events.jsonl
region_monitor/region_windows.jsonl
region_monitor/region_vocab.json
region_monitor/process_lifecycle.jsonl
region_monitor/vma_refresh_stats.json
region_monitor/region_monitor_errors.jsonl
region_monitor/region_monitor_summary.md
```

稀疏 region 向量保存在 `region_windows.jsonl`，CSV 只用于可选窗口级标量导出。

## 运行 capability check

```bash
bash runtime_monitor/scripts/check_region_monitor_capabilities.sh
```

如果输出 `SUPPORTED_NEEDS_ROOT`，说明内核接口存在但当前用户权限不足，需要在宿主机终端通过 `sudo` 运行 smoke。

## 运行 smoke

```bash
sudo bash runtime_monitor/scripts/run_region_monitor_smoke.sh \
  --app WPS \
  --duration 30 \
  --output-dir outputs/region_monitor_smoke
```

smoke 不启动 MGLRU Apply，不写 `lru_gen_pages`。

## 后续对齐

后续可以把 `region_windows.jsonl` 的 `window_start_ns/window_end_ns` 与 automation 的 `OP_START/OP_DONE`、Runtime Monitor 的 `foreground_epoch_id` 对齐，训练操作识别器。当前版本只采集和标准化 region，不训练模型。

## 当前状态

- observe-only：是
- `protection_eligible`：false
- ready_for_operation_collection：取决于宿主机 DAMON/tracefs 权限
- ready_for_apply：false

