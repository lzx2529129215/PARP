# Native / APPLY reclaim-bin 消融实验进度

状态：`COMPLETE_SEE_FINAL_REPORT`

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

## 完整 APPLY 内核实测进度

本轮已确认启动的是修正后的完整 APPLY `#10`，内核 release 为
`6.17.13-parp-lzx-v4.2-apply-reclaim-bin`，配置 SHA256 为
`2b17262e80c14302ac64ee7ceded6d055db011e941b3534f356275a012326762`。

有效结果目录：

- B 完整 APPLY：`peak-full-combined-20260826_112334-6.17.13-parp-lzx-v4.2-apply-reclaim-bin` 的 `round-01/02`，以及补跑目录 `peak-full-combined-20260826_112841-6.17.13-parp-lzx-v4.2-apply-reclaim-bin` 的 `round-01`。
- C effective-tier-only：`peak-full-effective-20260826_113058-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`。
- D Tier2 + reclaim-bin：`peak-full-tier2-20260826_113507-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`。

B 的种子 `20260823` 首次运行因 LibreOffice “Tip of the Day” 模态窗口抢占焦点而标记为 `INVALID`；随后不修改 test 代码，严格重放同一 Native 计划并得到 `VALID_DIAGNOSTIC`。无效轮保留审计但不进入均值。A/B/C/D 的三份计划已经逐轮做 canonical SHA256 比较，全部相同。

### A/B/C/D 三轮有效均值

| 组别 | PageFault | Major | Refault file | pgscan | pgsteal | Direct scan | Direct steal | Memcg calls | Memcg ms | OOM | OOM kill | Failure | Launch mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A Native | 4,504,657.33 | 1,382.00 | 521,897.00 | 1,406,115.00 | 1,333,986.33 | 1,406,115.00 | 1,333,986.33 | 9,216.67 | 658.35 | 1.00 | 1.00 | 1.00 | 3,775.29 |
| B Full APPLY | 4,502,784.00 | 1,535.67 | 476,121.00 | 2,001,622.00 | 1,350,961.00 | 457,192.33 | 14,111.00 | 2,888.67 | 1,355.06 | 1.33 | 1.00 | 1.00 | 2,617.46 |
| C Effective only | 4,500,621.67 | 903.00 | 448,705.33 | 1,194,863.67 | 1,194,237.33 | 1,194,863.67 | 1,194,237.33 | 6,442.00 | 302.76 | 1.00 | 1.00 | 1.00 | 2,120.73 |
| D Tier2 + reclaim-bin | 4,499,481.00 | 899.33 | 443,148.00 | 1,195,711.33 | 1,190,318.00 | 454.33 | 2.33 | 1,230.67 | 277.01 | 1.67 | 1.00 | 1.00 | 2,107.39 |

相对 Native，B 的 PageFault 仅下降 `0.04%`，direct scan 下降 `67.49%`，但总 pgscan 增加 `42.35%`、major fault 增加 `11.12%`、memcg reclaim 总耗时增加 `105.83%`，OOM kill 没有下降。因此目前不能声称完整 APPLY 达到整体优化目标。

D 的 direct scan 相对 Native 下降 `99.97%`，而 C 只下降 `15.02%`，说明 direct reclaim 路径转移主要来自 Tier2/reclaim-bin 组合。D 的总 pgscan 下降 `14.96%`、major fault 下降 `34.93%`、memcg reclaim 总耗时下降 `57.92%`，但 OOM kill 仍未下降。

### reclaim-bin 生效证据

完成 B/C/D 后的启动累计统计：

```text
lookups 67437
policy_hits 67375
inherited_hits 67368
context_hits 0
headroom_hits 67375
scored 67375
fallbacks 62
```

`scored` 与 `inherited_hits` 大幅增长，证明动态 systemd 子 cgroup 已通过最近启用祖先进入 reclaim-bin scorer；层级修复生效。`context_hits=0` 同时表明当前常驻服务是纯观测服务，本轮 scorer 只有 cgroup headroom 信号，没有应用预测上下文。因此这些结果仍是诊断数据，不能代表完整 LSTM 在线策略收益。

随后已启动 `6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin #11`，完成 E 组三轮编译宏消融，并生成 A/B/C/D/E 最终报告。

## 最终完成

E 组已在 `6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin #11` 上完成三轮有效结果；宏、符号和 debugfs 接口均确认不存在。A/B/C/D/E 的最终汇总、逐轮数据、PSI、reclaim-bin 单独贡献、局限和验收判定见：

`/home/lzx/Desktop/PARP/test_reports/native-v4.2-reclaim-bin-ablation-final-20260826-lzx.md`
