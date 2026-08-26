# 独立内存实验测试模块

`test` 是可独立安装和运行的 Linux 内存实验模块。它把原 `zhj` 的可信测量、
有效性检查和配对统计能力，与现有 PARP/LSTM 验收脚本放在同一模块中。

测量核心 `memsched_exp` 不依赖当前 `v4.1-parp` 内核，也不要求固定内核版本。
它默认只使用标准 Linux 接口并按能力发现：

- `/proc`、PSI 和 cgroup v2 的系统/应用快照；
- 可选 eBPF tracepoint 的 Direct Reclaim、kswapd 和 OOM 事件；
- 冷缓存、进程身份、cgroup 端点和事件完整性验证；
- schema-v4 manifest、环境/工作负载指纹和不可变原始数据；
- Baseline/Candidate 严格配对、描述统计、bootstrap 95% CI 和显著性结论。

PARP debugfs、LSTM 和 GUI 自动化都属于可选适配层，不是测量核心的安装或测试依赖。

## 目录

```text
test/
├── memsched_exp/        通用采集、schema、报告和配对统计核心
├── tests/               核心单元测试与通用 Linux 集成冒烟
├── scripts/             预检、采集器和实验场景
├── bpf/                 可选 reclaim/OOM eBPF tracepoint 程序
├── workloads/           Acclaim/AppFlow/Fleet 合成工作负载
├── configs/             GUI 参考配置
├── docs/                指标、接口和参考实验设计
├── *-lzx.py             PARP/LSTM 专用验收适配层
└── pyproject.toml/setup.cfg  独立 Python 包入口
```

## 安装与核心验证

```bash
cd /path/to/huawei_mem/test
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python3 -m unittest discover -s tests -v
bash scripts/preflight.sh --profile core
```

默认 `core` 预检不会检查 PARP，也不会要求 `uname -r` 是某个固定版本。正式实验需要
冻结内核版本时显式指定：

```bash
bash scripts/preflight.sh --profile core --require-kernel-prefix 6.17
```

可选预检配置：

```bash
bash scripts/preflight.sh --profile gui
bash scripts/preflight.sh --profile bpf
bash scripts/preflight.sh --profile all
```

缺少可选 tracepoint 时，核心仍可使用 vmstat/cgroup 回退指标；要求精确 eBPF 事件的
`bpf` 或 `all` 配置会拒绝运行。

Ubuntu 安装并实机验证 eBPF 采集：

```bash
sudo apt install bpftrace
bash scripts/preflight.sh --profile bpf
PYTHONPATH=. bash scripts/run_bpf_collector.sh 5 results/bpf-smoke
```

采集脚本会自动识别 distro 内核以及 source/output 分离的自编译内核头文件。内核
reclaim/OOM 记录来自真实 eBPF tracepoint；生命周期标记由用户态脚本写入，以兼容
移除了 `BEGIN/END` trigger 符号的 bpftrace 0.14 发行版。

## 独立采集协议

自动化程序和采集器通过四阶段协议解耦：

```text
collector_ready
→ workload_start
→ workload_stop
→ collector_done
```

因此外部自动化只负责应用动作，不需要直接读取内核计数器；采集核心也不需要知道
当前运行的是 PARP、原生 MGLRU 或其他候选策略。

通用 Linux 集成冒烟：

```bash
cd /path/to/huawei_mem/test
PYTHONPATH=. python3 tests/integration/linux_pipeline.py \
  --output results/integration-smoke
```

需要同时验证当前 cgroup 的 memory/cpu/io 端点时追加 `--current-cgroup`。任一端点缺失时
该严格检查会失败并保留原因，不会把缺失指标填成 0。

桌面会话重启后，可重新启用仅当前启动周期有效的 accounting，并在隔离 cgroup 中完成
严格冒烟：

```bash
sudo -v
bash scripts/enable_runtime_accounting.sh
bash scripts/run_strict_cgroup_smoke.sh
```

这些命令不写永久 systemd 配置；运行时属性在重启后自动失效。

## Baseline/Candidate 配对比较

正式轮次至少提供 variant、固定 seed、repetition 和 cache state。策略字段可以只是
外部实验元数据，不要求存在任何专用内核接口：

```bash
EXPERIMENT_VARIANT=baseline \
EXPERIMENT_SEED=20260814 \
EXPERIMENT_REPETITION=1 \
KERNEL_COMMIT='<baseline commit or build id>' \
POLICY_MODE=off \
OUTPUT_DIR=results/paired/acclaim-bg8/baseline-r01 \
bash scripts/scenarios/run_acclaim.sh 8

EXPERIMENT_VARIANT=candidate \
EXPERIMENT_SEED=20260814 \
EXPERIMENT_REPETITION=1 \
KERNEL_COMMIT='<candidate commit or build id>' \
POLICY_MODE=apply \
MODEL_PROVENANCE='<model id or hash>' \
OUTPUT_DIR=results/paired/acclaim-bg8/candidate-r01 \
bash scripts/scenarios/run_acclaim.sh 8
```

生成逐轮 CSV 和配对报告：

```bash
python3 -m memsched_exp.report \
  --root results/paired \
  --output results/paired/runs.csv

python3 -m memsched_exp.compare \
  --root results/paired \
  --output results/paired/comparison.json \
  --markdown results/paired/comparison.md
```

比较器只接受 `场景 + seed + repetition + cache state + workload hash` 一致、环境指纹
一致且两侧均有效的配对。每个指标同时报告 mean、median、sample standard deviation、
P90/P95、改善率、配对差 bootstrap 95% CI，以及：

- `significant_improvement`：95% CI 不跨 0 且方向有利；
- `significant_regression`：95% CI 不跨 0 且方向不利；
- `no_significant_difference`：95% CI 跨 0；
- `not_evaluable`：没有足够的有效值。

## 可选 PARP 适配器

只有需要把 manifest 绑定到真实 PARP debugfs 状态时才启用：

```bash
POLICY_ADAPTER=parp \
POLICY_DEBUGFS_ROOT=/sys/kernel/debug/parp \
POLICY_MODE=apply \
APPLY_COMPILED=true \
MODEL_PROVENANCE='<model id or hash>' \
bash scripts/scenarios/run_acclaim.sh 8
```

也可以单独验证接口：

```bash
bash scripts/preflight.sh --profile core --policy-adapter parp
```

未设置 `POLICY_ADAPTER=parp` 时，代码不会读取 `/sys/kernel/debug/parp`。

## PARP/LSTM 专用验收

原有 LZX 验收脚本保留在本目录根部，详细说明见
[PARP/MGLRU 验收实验](README-lzx.md) 和 [PARP 指标闭环实验设计](实验设计-lzx.md)。
它们是可选上层场景，不影响 `memsched_exp` 作为独立测量模块运行。

## 参考场景与文档

- `scripts/scenarios/run_acclaim.sh`：前后台回收与 refault；
- `scripts/scenarios/run_appflow.sh`：大文件冷启动和缓存真实性验证；
- `scripts/scenarios/run_fleet.sh`：托管运行时对象工作集代理；
- [测量系统接口手册](docs/MEASUREMENT_SYSTEM_GUIDE.md)；
- [指标字典](docs/METRICS.md)；
- [Linux 6.17 参考实验设计](docs/EXPERIMENT_DESIGN.md)。

Linux 6.17 是参考实验配置，不是核心代码依赖。不同内核缺失的指标必须标为不可用，
不能用 0 填充，也不能与具有完整数据源的轮次混为同一正式比较。
