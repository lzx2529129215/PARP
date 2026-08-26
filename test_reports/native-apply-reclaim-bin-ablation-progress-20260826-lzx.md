# Native / APPLY reclaim-bin 消融实验进度

状态：`RECLAIM_HIERARCHY_FIXED_AWAITING_FULL_APPLY_REBOOT`

更新时间：2026-08-26（Asia/Shanghai）

## 实验目标

在相同 OOM-THRESHOLD 场景、相同随机种子和 Native 生成的同一份 `scenario-plan.json` 下，对比 Linux 6.17.13 Native、完整 APPLY（effective-tier + Tier2 + reclaim-bin）以及功能消融组。

## 已准备内核

| 实验内核 | release | 关键配置 | vmlinuz SHA256 | config SHA256 |
|---|---|---|---|---|
| Native | `6.17.13-native-6.17.13` | 不包含 `CONFIG_PARP*` | `2544d0f7e475d6b83f642c9d1c0fdcbef103becfa791c43cd1c109359d7b0cd0` | `9fc0bf1e8ef9b3e6395c5594490d371c0f2ebe5fc8f809726931c8c42aa866da` |
| 完整 APPLY | `6.17.13-parp-lzx-v4.2-apply-reclaim-bin` | effective-tier=y、Tier2=y、reclaim-bin=y | `711df8e481038f0d090abe14c6e4a31cf4a08db3a5830ef4cbf46c455e6efd0d` | `2b17262e80c14302ac64ee7ceded6d055db011e941b3534f356275a012326762` |
| reclaim-bin 编译消融 | `6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin` | effective-tier=y、Tier2=y、reclaim-bin=n | `5b24e1e309eb918176a7c40b026846d36169ccfe27a4dc2cc3599d265d25521c` | `09ad8ee617b42e3df23a4d776b1fa187cffb81cddf5c027fc5a35a85166e43da` |

Native 源码固定为上游 v6.17.13 提交 `6609c4d49ebe220a5c40d3105c3f0e68f569ba1a`。完整 APPLY 与 reclaim-bin 编译消融的非本地版本号、非 reclaim-bin 配置已经逐项比较，无其他配置差异。

## 固定实验输入

- 配置：`/home/lzx/Desktop/PARP/test/test/parp-oom-threshold-config-lzx.json`
- profile：`full`
- suite：`peak`
- 基准种子：`20260821`
- 三轮实际种子：`20260821`、`20260822`、`20260823`
- 每轮步骤：30
- 测试 cgroup：`memory.max=8 GiB`、`memory.swap.max=0`
- OOM-THRESHOLD 压力：6656 MiB
- 配对方式：Native 先生成计划，其余各组严格 replay Native 的计划

## 消融矩阵

| 组别 | 内核 | effective-tier | Tier2 | reclaim-bin | 用途 |
|---|---|---:|---:|---:|---|
| A | Native | 0 | 0 | 0 | 真正上游基线 |
| B | 完整 APPLY | 1 | 1 | 1 | 完整优化效果 |
| C | 完整 APPLY | 1 | 0 | 0 | effective-tier-only |
| D | 完整 APPLY | 0 | 1 | 1 | Tier2 + reclaim-bin；消融 effective-tier |
| E | no-reclaim 内核 | 1 | 1 | 0 | 单独消融 reclaim-bin |

其中 C/D 使用运行时开关消融，E 使用编译宏消融，避免把 reclaim-bin 的编译结果误当成关闭状态。

## 待执行顺序

1. 启动 Native，执行 A 并保存三轮计划和原始指标。
2. 启动完整 APPLY，依次执行 B、C、D，全部 replay A 的计划。
3. 启动 no-reclaim 内核，执行 E，replay A 的计划。
4. 汇总 PageFault、major/minor fault、direct reclaim、OOM、PSI、延迟、失败峰值，并生成最终 Markdown 报告。

## Native 实测进度

Native 原始目录：

`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-native-20260826_102359-6.17.13-native-6.17.13`

种子 `20260822` 首次执行在第 22/30 个 case 后发生 VLC 窗口聚焦失败，因此该轮标记为 `INVALID` 并保留原始数据。随后严格重放同一份计划（SHA256 `5b32a82f49fa8d89ec2eb17824c791cd57717543dc4cae96b9d4f382370460f8`），补跑结果为 `VALID_DIAGNOSTIC`：

`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-native-20260826_102923-6.17.13-native-6.17.13`

最终 Native 有效轮次映射：

| 种子 | 有效结果 | plan SHA256 |
|---|---|---|
| 20260821 | 原始 `round-01` | `afb4f345a3b7e538f31d8f87f821be9e91bd86f5473f5cb9a4e9f5382c598e89` |
| 20260822 | 补跑 `round-01` | `5b32a82f49fa8d89ec2eb17824c791cd57717543dc4cae96b9d4f382370460f8` |
| 20260823 | 原始 `round-03` | `0ae111c45716695a575b3bab3100258b8624f5be078f046aa093c79d9129e7ff` |

三轮 Native 有效结果平均值：

| 指标 | 平均值 |
|---|---:|
| cgroup PageFault | 4,504,657.33 |
| cgroup major PageFault | 1,382.00 |
| direct reclaim 扫描页 | 1,406,115.00 |
| direct reclaim 回收页 | 1,333,986.33 |
| memcg reclaim 次数 | 9,216.67 |
| memcg reclaim 总耗时 | 658.350 ms |
| OOM / OOM kill | 1.00 / 1.00 |
| 失败峰值总数 | 1.00 |

三轮均触发了 cgroup 内的受控 OOM，宿主机 OOM 为 0，指标采集和 trace 配对均有效。

## reclaim-bin 覆盖修正

首次进入完整 APPLY `#8` 后，运行时核验发现测试父 slice 为 `memory.tier2_enabled=1`，但 systemd 创建的应用和压力子 scope 均为 0。旧 scorer 只检查页面所属的精确子 cgroup，因此该次 preliminary combined 运行实际回退到原生随机 bin，已停止并明确排除，不纳入任何比较。

源码现已改为：从叶子 cgroup 向上查找最近启用的 Tier2 祖先，使用该祖先的 headroom 策略，同时仍以叶子 cgroup 查询应用先验。新增 `/sys/kernel/debug/parp/reclaim_bin_stats`，正式 APPLY 轮次要求 `scored` 增长，且本测试的子 scope 结构要求 `inherited_hits` 增长。

修正后内核：

- 完整 APPLY：`#10`，reclaim-bin scorer 和统计符号存在。
- no-reclaim 消融：`#11`，reclaim-bin 配置、scorer 符号和统计接口均不存在。
- 两份配置除 `CONFIG_LOCALVERSION` 和 `CONFIG_PARP_RECLAIM_BIN_SCORE` 外逐项一致。
- 可复现补丁：`/home/lzx/Desktop/PARP/lzx/kernel/v4.2/patches/0005-reclaim-bin-hierarchy-and-stats.patch`

GRUB 下一次启动已经设置为完整 APPLY：`6.17.13-parp-lzx-v4.2-apply-reclaim-bin`。
