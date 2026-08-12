# PARP / MGLRU r9 基线指标说明

## 1. 结果身份

- 采集内核：`6.17.13-parp-effective-tier-shadow-657a65b0a-r9`
- 采集日期：`2026-08-12`
- 内核模式：Shadow，`apply_compiled=0`
- 模型来源：`ENGINEERING_FIXTURE_UNTRAINED`
- 结果状态：`CURRENT_R9_DIAGNOSTIC_COMPLETE`
- 验收状态：`NOT_EVALUABLE_SHADOW_NO_APPLY`

这组数据是优化前的诊断基线。Shadow 内核只计算和记录预测信息，不执行页面保护、升级或降级动作，因此不能仅凭这组数据宣布达到改善目标。正式验收需要在同源 Native/OFF 和 Apply 内核上复用完全相同的 seed 与场景，再按配对结果计算改善率。

## 2. 原始验收指标

### 2.1 应用内存冷热精准识别

- 正式目标：由内存导致的 PageFault 次数相对优化前降低 `20%`。
- 挑战目标：PageFault 次数相对优化前降低 `30%`。
- 环境要求：启用 swap；所有应用的受控内存总量为物理内存的 `150%～200%`。
- 测试要求：随机切换应用窗口，优化前后复用相同场景，重复 `10` 轮并比较平均值。
- 正式统计口径：受控内存 sidecar PID 的 `exceptions:page_fault_user` tracepoint。
- 交叉复核口径：测试 slice 的 `pgfault` 和 `pgmajfault` 增量。

### 2.2 内存错峰调度

- 正式目标：应用启动失败、低内存弹窗和应用被 OOM 查杀的总次数相对优化前降低 `30%`。
- 环境要求：16 GiB 级别内存；单个应用峰值不超过物理内存；所有应用日常用量总和不超过物理内存；并发峰值总和超过物理内存 `20%` 以上。
- 测试要求：累计测试步骤不少于 `100`，重复 `3` 轮并比较平均值。
- 统计口径：`启动/自动化失败 + 低内存弹窗 + 测试 cgroup oom_kill`。

## 3. 当前 r9 基线

### 3.1 冷热 PageFault 基线

| 项目 | 当前基线 | 轮次范围 | Apply 达到20%所需上限 | Apply 达到30%所需上限 |
|---|---:|---:|---:|---:|
| `page_fault_user` | `485051.9` 次/轮 | `451672～503242` | `388041.52` | `339536.33` |
| slice `pgfault` | `7176296.5` 次/轮 | `7139702～7192663` | `5741037.2` | `5023407.55` |
| slice `pgmajfault` | `1523.5` 次/轮 | `1409～1694` | `1218.8` | `1066.45` |

- 有效轮次：`10/10`
- 有效应用切换步骤：`240`
- trace 丢失：`0`
- 受控逻辑内存：物理内存的 `150%`

真实refault基线已经由每轮 `monitor.csv` 的首尾差值恢复：

| 指标 | 均值/轮 | 最小 | 最大 |
|---|---:|---:|---:|
| `workingset_refault_file` | `215392.1` | `125345` | `302543` |
| `workingset_refault_anon` | `53.1` | `0` | `221` |

其中 `page_fault_user` 是验收主指标；slice 的两个数值只作GUI应用和 sidecar 总体行为的交叉复核。目标上限由 `基线 × (1 - 目标改善率)` 换算。正式改善率应按以下公式计算：

```text
(Native/OFF 基线均值 - Apply 均值) / Native/OFF 基线均值 × 100%
```

### 3.2 峰值调度基线

| 项目 | 当前均值 |
|---|---:|
| 启动失败 | `0.0` 次/轮 |
| 低内存弹窗 | `0.0` 次/轮 |
| 测试 cgroup OOM kill | `0.0` 次/轮 |
| 峰值异常总数 | `0.0` 次/轮 |
| `page_fault_user`（辅助数据） | `1425034.33` 次/轮 |
| slice `pgfault`（辅助数据） | `7574673.33` 次/轮 |
| slice `pgmajfault`（辅助数据） | `4233.67` 次/轮 |

- 有效轮次：`3/3`
- 有效步骤：`300`
- trace 丢失：`0`
- 日常内存比例总和：`65%`
- 并发峰值比例总和：`125%`

峰值场景真实refault为：`workingset_refault_file=154880.33` 次/轮，`workingset_refault_anon=46.33` 次/轮。测试cgroup `oom=0`、`oom_kill=0`，宿主 `oom_kill=0`。

本轮使用 `metrics_schema_version=2`，`workingset_activate/restore`、`pgscan/pgsteal`、direct/kswapd扫描回收量、direct/memcg reclaim延迟和kswapd CPU时间都已获得真实数值；详细均值、范围与各轮值见合并报告。

当前峰值异常基线为 `0`，改善率分母为0，因此不能评价“降低30%”。这表示现有场景能够安全完成，但压力还没有校准到会稳定产生非零异常的边界。下一步应在宿主安全阈值不变的前提下逐级增强应用组合或峰值驻留量，先得到可重复的非零 Native/OFF 基线，再与 Apply 配对比较。

## 4. 当前机器运行条件

重启后已经满足以下内核条件：

- 当前内核为 r9 effective-tier Shadow。
- `/sys/fs/cgroup` 使用统一 cgroup v2，memory controller 可用。
- `exceptions:page_fault_user` 可用。
- `parp:parp_effective_tier_decision` 可用。
- swap、16 GiB 内存级别、磁盘空间和所需应用均通过预检。

通过 SSH 启动实验时仍须保证图形会话环境中存在有效的 `DISPLAY` 和 `XAUTHORITY`。否则预检会以 `x11_display=false` 阻止自动操作GUI应用。

## 5. 复现命令

```bash
cd /home/lzxxxxxx/桌面/huawei/myself-kswapd

python3 test/test-parp-acceptance-lzx.py -v
python3 test/parp-acceptance-lzx.py preflight --profile smoke --suite all

python3 test/parp-acceptance-lzx.py run --profile full --suite hotcold --seed 20260812
python3 test/parp-acceptance-lzx.py run --profile full --suite peak --seed 20260812
```

本次基线的本地合并报告位于：

```text
lzx/tool/outputs/parp_acceptance/current-baseline-20260812-lzx/current-baseline-metrics-lzx.md
lzx/tool/outputs/parp_acceptance/current-baseline-20260812-lzx/current-baseline-metrics-lzx.json
```
