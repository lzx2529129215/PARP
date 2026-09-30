# APP-WSS-eBPF

一个面向 Linux **target cgroup** 的应用级内存行为采集框架。它在一个固定操作窗口内同时输出 strict real WSS、legacy WSS、RSS、PageFault、cgroup memory、PSI 和系统内存指标。

采集器与具体应用无关。仓库中的 WPS 18 操作只是可选自动化示例；Chrome、Firefox、mpv、会议软件、自研 Python/C/C++ 程序或其他可被放入 cgroup 的应用都可接入自己的 trigger。

> `real_wss_*` 是 `clear_refs + smaps Referenced` 的严格窗口测量/proxy，不是逐 PTE 直接读取 Accessed bit 的绝对 ground truth。

## 目录

| 路径                                                     | 中文作用                                 |
| -------------------------------------------------------- | ---------------------------------------- |
| `scripts/phase8_collector_real_wss_split.py`             | 最新 strict real-WSS 采集器              |
| `scripts/phase8_collector.py`                            | legacy Phase-8 基线采集器                |
| `ebpf/app_fault.bpf.c`                                   | 内核侧 PageFault eBPF 程序               |
| `ebpf/app_fault_user.c`                                  | libbpf 用户态 loader/周期读取器          |
| `scripts/check_environment.py`                           | 依赖、内核 hook 和 procfs 环境检查       |
| `scripts/quick_start.sh`                                 | 不依赖 WPS 的最小端到端验证              |
| `scripts/phase7_workload.py`                             | quick start 使用的小型内存 workload      |
| `scripts/setup_app_cgroup.sh`、`launch_app_in_cgroup.sh` | 创建 cgroup、让应用出生即入组            |
| `automation/`                                            | 可选 WPS 18-operation 与 90-event 示例   |
| `dataset/representative_operations.csv`                  | 18 项代表性操作定义                      |
| `docs/`                                                  | 架构、指标、环境和验证说明               |
| `examples/`                                              | 来自真实验证的已脱敏小型样例，不含大文件 |

## 采集架构

```text
Application / automation trigger
              │
              ▼
        target cgroup
              │
    ┌─────────┼────────────────────────┐
    │         │                        │
    ▼         ▼                        ▼
PID + procfs  eBPF                     cgroup / PSI
WSS / RSS     PageFault                 memory statistics
    │         │                        │
    └─────────┴────────────┬───────────┘
                             ▼
       phase8_collector_real_wss_split.py
                             ▼
       JSON summary + periodic CSV + eBPF raw CSV
```

## strict real WSS 的准确时序

```text
获取 T0 target-cgroup PID
  → 对每个 T0 PID 写 /proc/<pid>/clear_refs = 1
  → 记录 clear 成功 PID
  → 开始 monotonic operation window
  → 执行 -- 后的 trigger
  → 窗口结束
  → 读取 smaps 的 Referenced
  → 只汇总 clear 成功的 T0 PID
  → 输出 Total / Anon / File / Shmem / Other / Unknown
```

T1 才出现的 PID 会记录在 `real_wss_new_t1_pids_excluded`，但不会混入 strict real WSS，因为其操作前引用状态未被本窗口清零。Anon/File 通过 VMA pathname、`Rss` 和 `Anonymous` 等信息进行 VMA/residency-assisted 分类；混合 File+COW VMA 可能进入 `Unknown`。

legacy `wss_referenced_*` 保留原语义：对 T0∪T1 可读 PID 的 post-window smaps 做汇总。因此它与 strict real WSS 的差异本身是有意义的审计信息。

## 环境要求

通用 collector：Linux、Python 3.8+、cgroup v2、可用的 `sudo -n`/root 权限、clang/LLVM、gcc、make、pkg-config、libbpf development files、libelf 和 zlib development files。内核需能使用 `exceptions/page_fault_user` tracepoint 及本版本的 kprobe symbols。

WPS 示例另外需要 WPS Office、`xdotool`、活动 GUI session、PPTX/XLSX/DOCX/图像测试文件，以及当前 GUI 会话导出的 `DISPLAY`（必要时也包括 `XAUTHORITY`、`XDG_RUNTIME_DIR`、`DBUS_SESSION_BUS_ADDRESS`）。通用 collector 不需要 WPS。

先执行：

```bash
python3 scripts/check_environment.py
```

`[WARN]` 表示 optional 或与内核版本相关的能力不可确认；Linux、Python、cgroup v2 是强制前提，缺失会出现 `[FAIL]`。

## 编译与最小验证

```bash
git clone <repository-url>
cd APP-WSS-eBPF
make
```

预期构建产物：

```text
build/app_fault.bpf.o
build/app_fault_collector
```

在具备 sudo/cgroup/eBPF 权限的 Linux 主机，可运行不依赖 GUI 的端到端验证：

```bash
bash scripts/quick_start.sh
```

它创建一个小型匿名内存 workload 和临时 cgroup，在 5 秒窗口内触发访问，并验证 summary 含有 `real_wss_total_bytes`、`rss_total_bytes`、`page_fault_total_count`。

## 运行通用 collector

先将应用从出生时放入新 cgroup：

```bash
scripts/launch_app_in_cgroup.sh \
  --name my-app --run-id demo-001 -- \
  /path/to/my-application --its-arguments
```

脚本会打印 `cgroup=/sys/fs/cgroup/...`。把该绝对 cgroup 路径提供给 collector：

```bash
python3 scripts/phase8_collector_real_wss_split.py \
  --project "$(pwd)" \
  --cgroup /sys/fs/cgroup/<your-cgroup> \
  --run-id demo-001 \
  --test-type CUSTOM \
  --application my-app \
  --operation-id op001 \
  --operation-name "custom operation" \
  --output-dir ./runs/demo-001 \
  --window-ms 5000 \
  --interval-ms 500 \
  -- \
  bash ./my_automation.sh
```

参数说明：

| 参数                                  | 含义                                                   |
| ------------------------------------- | ------------------------------------------------------ |
| `--project`                           | 仓库根目录；用于定位 `build/` 下 eBPF loader 和 object |
| `--cgroup`                            | target cgroup 的绝对路径                               |
| `--run-id`                            | 本次运行的可追踪 ID                                    |
| `--application`                       | 应用标签                                               |
| `--operation-id` / `--operation-name` | 当前操作标识和人类可读名称                             |
| `--output-dir`                        | 必须是尚不存在的本次输出目录                           |
| `--window-ms` / `--interval-ms`       | 操作窗口与周期采样间隔                                 |
| `--` 后                               | collector 在 clear 后、窗口内执行的 trigger 命令       |

不要把一个已经运行很久的 PID 直接当作“出生即入组”的替代。已有进程可写入 `cgroup.procs` 迁移，但历史 cgroup 记账与窗口前状态需要自行解释。

## 接入自己的自动化

collector 不要求知道自动化内部逻辑，只要求：

1. 应用进程处在指定 target cgroup；
2. collector 已启动并完成 T0 clear；
3. 操作命令作为 `--` 后的 trigger 执行。

trigger 可以是 Python、shell、xdotool、Playwright、Selenium 或自研控制程序：

```bash
python3 scripts/phase8_collector_real_wss_split.py ... -- \
  python3 my_automation.py --single-op 3
```

WPS 18-operation 示例使用 `automation/wps_representative_automation.py`。复制 `config/example.env` 的变量到你的私有环境后，先确认：

```bash
python3 -m automation.wps_representative_automation --all --dry-run
```

大型文档不在仓库中；请配置 `WPS_SAMPLES_DIR` 或每个 `PHASE9_*` 路径。WPS 示例不负责启动 GUI 程序，使用者需在当前 GUI session 中启动它。

## 输出文件

| 文件                                            | 用途                                          |
| ----------------------------------------------- | --------------------------------------------- |
| `phase8_summary.json`                           | 单个窗口的最终汇总，优先用于下游分析          |
| `phase8_summary.csv`                            | 与 JSON 相同的单行 CSV 形式                   |
| `phase8_samples.csv`                            | procfs/cgroup/PSI 周期采样                    |
| `lightweight_features_v2.csv`                   | eBPF PageFault 的原始周期采样                 |
| `collector_stdout.log` / `collector_stderr.log` | loader 诊断证据                               |
| `metadata.json`                                 | 命令、T0/T1 PID、clear/smaps/trigger 的元数据 |

## 指标速查

### WSS

| 字段                                       | 中文含义                        | 来源                                    |
| ------------------------------------------ | ------------------------------- | --------------------------------------- |
| `real_wss_total_bytes`                     | 严格窗口内重新被引用的总内存    | T0 clear 成功 PID 的 `smaps:Referenced` |
| `real_wss_anon_bytes`                      | 严格窗口匿名 Referenced         | smaps + VMA 分类                        |
| `real_wss_file_bytes`                      | 严格窗口文件 Referenced         | smaps + VMA 分类                        |
| `real_wss_shmem_bytes`                     | 严格窗口共享内存 Referenced     | smaps                                   |
| `real_wss_other_bytes` / `unknown_bytes`   | 其他或无法严格分类的 Referenced | smaps                                   |
| `wss_referenced_total_bytes`               | legacy WSS Referenced 总量      | post-window smaps（T0∪T1）              |
| `wss_referenced_anon_bytes` / `file_bytes` | legacy Anon/File WSS            | smaps + VMA 分类                        |

### RSS 与 PageFault

| 字段                                                         | 中文含义                | 底层                         |
| ------------------------------------------------------------ | ----------------------- | ---------------------------- |
| `rss_total_bytes` / `rss_anon_bytes` / `rss_file_bytes` / `rss_shmem_bytes` | cgroup 内进程 RSS 聚合  | `/proc/<pid>/status`         |
| `page_fault_total_count`                                     | 用户态 PageFault 总次数 | `exceptions/page_fault_user` |
| `page_fault_anon_handler_count`                              | 匿名处理路径进入次数    | `kprobe/__vmf_anon_prepare`  |
| `page_fault_file_handler_count`                              | 文件处理路径进入次数    | `kprobe/filemap_fault`       |
| `page_fault_wp_handler_count`                                | 写保护/COW 路径次数     | `kprobe/do_wp_page`          |
| `page_fault_shmem_handler_count`                             | shmem fault 路径次数    | `kprobe/shmem_fault`         |

Anon/File PageFault 是 handler-path observation，**不能假设 `Total = Anon + File`**。PageFault 的 `*_4k_equivalent_bytes` 是 count×base page size 的派生单位，不表示 I/O 或新增 RSS。

### cgroup、PSI 与系统

`memory.current`、`memory.stat`、`memory.events`、`memory.pressure`、`/proc/pressure/memory` 和 `/proc/meminfo` 的相关字段都会写入 summary。完整字段、单位、测量性质和限制见 [docs/metrics.md](docs/metrics.md)。

## 底层接口与限制

| 类别                | 接口                                             |
| ------------------- | ------------------------------------------------ |
| strict WSS 清零     | `/proc/<pid>/clear_refs`                         |
| strict/legacy WSS   | `/proc/<pid>/smaps`                              |
| RSS                 | `/proc/<pid>/status`、`/proc/<pid>/statm`        |
| CPU/proc fault 辅助 | `/proc/<pid>/stat`                               |
| PID 枚举            | `cgroup.procs`                                   |
| cgroup memory       | `memory.current`、`memory.stat`、`memory.events` |
| PSI                 | `memory.pressure`、`/proc/pressure/memory`       |
| eBPF Total PF       | `exceptions/page_fault_user`                     |
| eBPF Anon/File      | `__vmf_anon_prepare`、`filemap_fault`            |

当前 WSS 没有使用 `BPF_MAP_TYPE_BITMAP`，没有遍历 PTE，也没有直接读取 PTE Accessed bit。eBPF 的 map 是配置、per-CPU 计数和 cgroup 过滤；它与 smaps WSS 读取是独立链路。

## 数据质量

重点检查：`data_quality_status`、`real_wss_quality_status`、`real_wss_ordering_ok`、`real_wss_t0_pid_count`、`real_wss_cleared_t0_pid_count`、`real_wss_t1_pid_count`、`real_wss_new_t1_pid_count`、`real_wss_missing_after_window_pid_count` 和 `smaps_error_count`。

- `PASS`：clear 全部成功、strict T0 PID 读取完整、trigger 成功、eBPF 时间戳正常；
- `WARN`：主采集完成，但可选压力稳定性等非 fatal 条件不满足；
- `FAIL`：clear/smaps/trigger/eBPF 窗口等核心条件失败。不要用 FAIL 行填补或估算有效样本。

## 已验证结果

在一轮 WPS 18 个代表性操作中，strict real WSS 有效 **18/18**，`clear_refs → operation → smaps` 时序正确 **18/18**，T0 clear 未覆盖 PID=0，strict PID smaps 读取失败=0，自动化失败=0。REP001 和 REP006 出现 T1-only PID，因此 legacy WSS 与 strict real WSS 有明显差异；这符合两种 PID 纳入语义的预期。

这只验证了通用 collector 机制在该 WPS 操作集下可用，**不等同于所有 Linux 应用均已验证通过**。其他应用应在自己的 cgroup、自动化和内核环境中执行验证。

## 进一步阅读

- [架构](docs/architecture.md)
- [指标定义](docs/metrics.md)
- [环境与权限](docs/environment.md)
- [18-operation 验证](docs/validation.md)
- [发布审计](RELEASE_AUDIT.md)
- [许可证占位](LICENSE_PLACEHOLDER.md)
