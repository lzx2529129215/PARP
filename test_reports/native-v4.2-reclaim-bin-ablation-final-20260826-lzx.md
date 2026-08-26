# Linux 6.17.13 Native 与 PARP v4.2 reclaim-bin 消融实验最终报告

状态：`DIAGNOSTIC_COMPLETE_NOT_ACCEPTED`

日期：2026-08-26（Asia/Shanghai）

## 结论摘要

本次完成了 Linux 6.17.13 Native、完整 APPLY、effective-tier-only、Tier2 + reclaim-bin、完整 APPLY 但编译去除 reclaim-bin 五组实验。每组采用相同的三个随机种子和逐项完全相同的 `scenario-plan.json`，最终共有 15 个有效轮次进入统计。

> 2026-08-26 事后审计修正：B/C/E 虽然把 effective-tier 运行时模式写成了 APPLY，但启动命令行没有 `parp_effective_tier_reserve=1`，所以 `metadata_reservation_requested=0`、`metadata_ready=0`。对应轮次最终均为 `scores=0`、`upgrade_pages=0`、`downgrade_pages=0`、`policy_promotions=0`，并出现 `metadata_missing`/`state_fault`。因此，旧数据不能证明 effective-tier 的收益或它与 Tier2/reclaim-bin 的负交互；C 也不能作为有效的 effective-tier-only 消融结果。

结论分为两部分：

1. reclaim-bin 功能验证通过。完整 APPLY 启动累计 `scored=67,375`、`inherited_hits=67,368`，证明动态 systemd 子 cgroup 已通过最近启用的 Tier2 祖先进入 scorer；no-reclaim 内核同时满足宏、符号和 debugfs 接口全部不存在。
2. 当前“请求 Full APPLY、实际 effective-tier 未动作”的 B 组没有达到交付指标。相对 Native，PageFault 只下降 `0.04%`，OOM kill 和失败峰值均为 `0%` 改善；虽然 direct scan 下降 `67.49%`、PSI stall 和启动代理延迟下降，但总 pgscan 增加 `42.35%`、major fault 增加 `11.12%`、memcg reclaim 总耗时增加 `105.83%`。

因此，本报告可以证明 Tier2/reclaim-bin 代码路径真实生效以及配对实验可执行，但不能证明 effective-tier 或真正的完整 APPLY 已经优化 PageFault/OOM。

## 实验输入与有效性

- 场景：OOM-THRESHOLD，`peak/full`。
- 物理内存：约 32 GiB。
- 实验 cgroup：`memory.max=8 GiB`、`memory.swap.max=0`。
- 匿名压力：6656 MiB，每轮 30 个自动化 case。
- 种子：`20260821`、`20260822`、`20260823`。
- 每组有效轮次：3；总有效轮次：15。
- 所有有效轮次的宿主 OOM：0。
- 所有有效轮次的 trace 丢失：0；trace 配对错误：0。
- 所有有效轮次均完成 30/30 个 case，状态为 `VALID_DIAGNOSTIC`。

三个 Native 计划的原始文件 SHA-256：

| 种子 | scenario-plan SHA-256 |
|---:|---|
| 20260821 | `afb4f345a3b7e538f31d8f87f821be9e91bd86f5473f5cb9a4e9f5382c598e89` |
| 20260822 | `5b32a82f49fa8d89ec2eb17824c791cd57717543dc4cae96b9d4f382370460f8` |
| 20260823 | `0ae111c45716695a575b3bab3100258b8624f5be078f046aa093c79d9129e7ff` |

A–E 每组对应 seed 的计划哈希均与上表一致。首次运行中出现的 Native `20260822`、Full APPLY `20260823`、no-reclaim `20260823` GUI 焦点失败轮均保留用于审计，但明确排除；补跑严格重放原计划，没有修改 test 代码。

## 内核与消融矩阵

| 组别 | 内核 | effective-tier 请求/有效动作 | Tier2 | reclaim-bin |
|---|---|---:|---:|---:|
| A Native | `6.17.13-native-6.17.13` | 0 | 0 | 0 |
| B Full APPLY | `6.17.13-parp-lzx-v4.2-apply-reclaim-bin #10` | APPLY / 0 | 1 | 1 |
| C Effective only | 同 B | APPLY / 0 | 0 | 0（路径不启用） |
| D Tier2 + reclaim-bin | 同 B | 0 | 1 | 1 |
| E Full no-reclaim | `6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin #11` | APPLY / 0 | 1 | 0（编译移除） |

内核文件证据：

| 内核 | vmlinuz SHA-256 | config SHA-256 |
|---|---|---|
| Native | `2544d0f7e475d6b83f642c9d1c0fdcbef103becfa791c43cd1c109359d7b0cd0` | `9fc0bf1e8ef9b3e6395c5594490d371c0f2ebe5fc8f809726931c8c42aa866da` |
| Full APPLY | `711df8e481038f0d090abe14c6e4a31cf4a08db3a5830ef4cbf46c455e6efd0d` | `2b17262e80c14302ac64ee7ceded6d055db011e941b3534f356275a012326762` |
| Full no-reclaim | `5b24e1e309eb918176a7c40b026846d36169ccfe27a4dc2cc3599d265d25521c` | `09ad8ee617b42e3df23a4d776b1fa187cffb81cddf5c027fc5a35a85166e43da` |

Full APPLY 与 Full no-reclaim 的完整配置只存在两处差异：`CONFIG_LOCALVERSION` 和 `CONFIG_PARP_RECLAIM_BIN_SCORE`。Native 与 Full APPLY 去除 `CONFIG_PARP*` 和 `CONFIG_LOCALVERSION` 后，配置符号相同；文本 diff 只剩新增 PARP Kconfig 菜单注释。

## 三轮有效均值：主要结果

| 组别 | PageFault | Minor | Major | Refault file | OOM | OOM kill | 失败峰值 | 启动均值 ms | 启动 P95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A Native | 4,504,657.33 | 4,503,275.33 | 1,382.00 | 521,897.00 | 1.00 | 1.00 | 1.00 | 3,775.29 | 5,045.63 |
| B Full APPLY | 4,502,784.00 | 4,501,248.33 | 1,535.67 | 476,121.00 | 1.33 | 1.00 | 1.00 | 2,617.46 | 3,897.15 |
| C Effective only | 4,500,621.67 | 4,499,718.67 | 903.00 | 448,705.33 | 1.00 | 1.00 | 1.00 | 2,120.73 | 3,129.31 |
| D Tier2 + reclaim-bin | 4,499,481.00 | 4,498,581.67 | 899.33 | 443,148.00 | 1.67 | 1.00 | 1.00 | 2,107.39 | 3,325.73 |
| E Full no-reclaim | 4,509,803.67 | 4,508,193.33 | 1,610.33 | 587,016.00 | 1.33 | 1.00 | 1.00 | 3,203.15 | 4,430.50 |

启动指标来自 X11 验证窗口出现时间，是启动就绪代理而不是 first-frame latency。

## 三轮有效均值：回收指标

| 组别 | pgscan | pgsteal | 效率 % | Direct scan | Direct steal | memcg 次数 | memcg 总耗时 ms | memcg P95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A Native | 1,406,115.00 | 1,333,986.33 | 96.02 | 1,406,115.00 | 1,333,986.33 | 9,216.67 | 658.350 | 0.175 |
| B Full APPLY | 2,001,622.00 | 1,350,961.00 | 81.15 | 457,192.33 | 14,111.00 | 2,888.67 | 1,355.062 | 1.099 |
| C Effective only | 1,194,863.67 | 1,194,237.33 | 99.95 | 1,194,863.67 | 1,194,237.33 | 6,442.00 | 302.762 | 0.107 |
| D Tier2 + reclaim-bin | 1,195,711.33 | 1,190,318.00 | 99.55 | 454.33 | 2.33 | 1,230.67 | 277.005 | 0.707 |
| E Full no-reclaim | 3,378,583.67 | 1,457,774.00 | 74.55 | 1,247,282.00 | 22,734.67 | 2,475.67 | 3,282.557 | 2.750 |

这里的 direct reclaim 主指标来自测试 cgroup 的 `pgscan_direct/pgsteal_direct`。`mm_vmscan_direct_reclaim_begin/end` 在该受控 memcg 回收路径中仍为 0，而 `mm_vmscan_memcg_reclaim_begin/end` 完整配对，因此不能用 direct tracepoint 的 0 覆盖 cgroup 的非零 direct reclaim 计数。

## 三轮有效均值：PSI 与资源

| 组别 | PSI some 峰值 avg10 | PSI full 峰值 avg10 | Some stall ms | Full stall ms | 最低 MemAvailable GiB | 单核 CPU % | 读取 MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| A Native | 1.833 | 1.180 | 416.866 | 272.540 | 17.408 | 27.836 | 1,347.98 |
| B Full APPLY | 1.880 | 1.270 | 216.847 | 146.089 | 17.539 | 31.188 | 1,491.54 |
| C Effective only | 0.790 | 0.743 | 288.260 | 194.429 | 17.674 | 24.810 | 715.56 |
| D Tier2 + reclaim-bin | 0.600 | 0.180 | 94.340 | 39.582 | 17.582 | 24.359 | 693.73 |
| E Full no-reclaim | 3.530 | 2.073 | 409.423 | 246.006 | 17.451 | 28.737 | 1,922.43 |

## 相对 Native 的变化

下表正数表示下降/改善，负数表示增加/退化。

| 组别 | PageFault | Major | Refault | pgscan | Direct scan | memcg 耗时 | PSI full stall | 启动均值 | OOM kill | 失败峰值 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B Full APPLY | +0.04% | -11.12% | +8.77% | -42.35% | +67.49% | -105.83% | +46.40% | +30.67% | 0.00% | 0.00% |
| C Effective only | +0.09% | +34.66% | +14.02% | +15.02% | +15.02% | +54.01% | +28.66% | +43.83% | 0.00% | 0.00% |
| D Tier2 + reclaim-bin | +0.11% | +34.93% | +15.09% | +14.96% | +99.97% | +57.92% | +85.48% | +44.18% | 0.00% | 0.00% |
| E Full no-reclaim | -0.11% | -16.52% | -12.48% | -140.28% | +11.30% | -398.60% | +9.74% | +15.15% | 0.00% | 0.00% |

PageFault 的所有变化均低于 `0.2%`，远低于目标，现阶段应视为没有实质改善。

## reclaim-bin 的诊断贡献：B 对比 E

B 与 E 具有相同的 effective-tier/Tier2 运行时设置，内核配置只差 reclaim-bin 宏和用于区分内核的 `LOCALVERSION`。以 E 为基准，启用 reclaim-bin 后：

| 指标 | B 相对 E |
|---|---:|
| PageFault | 下降 0.16% |
| Major fault | 下降 4.64% |
| Refault file | 下降 18.89% |
| pgscan | 下降 40.76% |
| pgsteal | 下降 7.33% |
| Direct scan | 下降 63.34% |
| memcg reclaim 次数 | 增加 16.68% |
| memcg reclaim 总耗时 | 下降 58.72% |
| PSI some/full stall | 下降 47.04% / 40.62% |
| 启动均值/P95 | 下降 18.29% / 12.04% |
| OOM kill / 失败峰值 | 0.00% / 0.00% |

由于两组 effective-tier 都没有有效动作，这个对比实际测到的是 Tier2 上下文中“有/无 reclaim-bin”的差异。结果说明 reclaim-bin 在当前诊断场景中减少了扫描、refault、stall 和回收耗时，但没有降低 OOM kill，也没有给 PageFault 带来有意义的改善。实验顺序没有交叉平衡，因此仍需交换 B/E 顺序复测来排除跨重启缓存和时间漂移。

## D 为何看起来优于 B，以及为什么不能归因为组合负交互

表面上，B 相比 D：

- pgscan 增加约 `67.40%`。
- memcg reclaim 总耗时增加约 `389.18%`。
- PSI full stall 增加约 `269.08%`。
- major fault 增加约 `70.76%`。

但这**不能**证明 effective-tier 抵消了 D 的提升，原因有两层：

1. effective-tier 在 B 中没有有效动作。其页面元数据只能通过启动参数提前预留；旧 `#10` 没有预留，却仍允许把模式写为 APPLY。最初少量候选产生 `metadata_missing`/`state_unstable`，随后状态故障使评分路径停止，最终所有有效轮次的 `scores`、升 tier、降 tier 和策略晋升均为 0。也就是说，B 并不是真正的 Full APPLY。
2. A/B/C/D/E 采用固定组间顺序，测试又有意不执行 `drop_caches`。每次重启后的首个 `20260821` 都明显较重：Native/B/E 的 major fault 分别为 `2,353`/`2,756`/`2,997`，读取量分别约为 `1,348`/`1,492`/`1,922 MiB`；同一 `#10` 启动中稍后运行的 C/D 已受前序应用启动和文件页缓存预热，读取量只有约 `716`/`694 MiB`。B 的三轮均值因此被冷启动的第一轮明显抬高。

去掉最明显的冷启动种子后，B 与 D 已很接近：seed `20260822/23` 的 pgscan 分别为 B `1,222,414/1,217,463`、D `1,193,360/1,196,788`，major fault 分别为 B `929/922`、D `903/899`。这更符合“同一套 Tier2/reclaim-bin 主路径，叠加缓存状态小差异”，而不是已证实的机制冲突。

D 中确实存在可验证的主动回收机制：例如 seed `20260821` 的 `pgscan_total=1,196,986`，其中 `pgscan_proactive=1,196,790`、`pgscan_direct=196`。Tier2 在严重压力前触发有界 memcg 回收，reclaim-bin 再按 cgroup 剩余水位对候选排序，因此把绝大多数扫描从任务同步承担的 direct reclaim 转移到了 proactive reclaim；这解释了 D 的 direct scan 和 PSI full stall 很低。与此同时 `context_hits=0`，所以本轮排序只使用 headroom，并没有使用 LSTM 应用预测上下文。

修复后，真正的 effective-tier 与 Tier2 仍可能发生机制交互，但必须在看到非零 `scores` 和升/降 tier 动作后才能判断：如果页面保护过强，Tier2 主动回收会为达到目标扫描得更深，形成扫描放大；如果降级判断错误，则会增加 refault/major fault；严重压力下的安全门控也可能抑制 tier 动作。这些是后续要用交叉平衡实验验证的假设，不是旧 B 数据已经证明的结论。

## 已实施修复（等待重启后运行时确认）

1. 内核的 effective-tier 模式入口已改为 fail-closed：只要 `metadata_ready!=1`，任何非 OFF 模式都返回 `-EOPNOTSUPP`，不再出现“模式显示 APPLY、实际无法评分”的假成功。
2. 验收 runner 在启用 effective-tier 前同时检查 `metadata_reservation_requested=1` 和 `metadata_ready=1`；缺失时前置检查直接 `BLOCKED`，并提示加入 `parp_effective_tier_reserve=1`。
3. 修复内核 `6.17.13-parp-lzx-v4.2-apply-reclaim-bin-meta #12` 已编译和安装。其 bzImage SHA-256 为 `6e782f4f02c73e4e4b35f51111959e4d1ff9e1c73f452fe0359a8ebdf0329da7`，配置 SHA-256 为 `ac733a688b593fd6d30c918aefcedf009b902e151d7386f89949eb5678638cda`。
4. GRUB 默认项已指向修复内核，命令行已加入 `parp_effective_tier_reserve=1`。尚需重启后确认 `metadata_ready=1`，再以非零评分/动作计数验证真实 Full APPLY。

## reclaim-bin 运行时证据

完整 APPLY `#10` 完成 B/C/D 后的启动累计值：

```text
lookups 67437
policy_hits 67375
inherited_hits 67368
context_hits 0
headroom_hits 67375
scored 67375
fallbacks 62
```

`scored` 和 `inherited_hits` 证明层级继承修复有效。`context_hits=0` 同时说明本轮 reclaim-bin 只使用 cgroup headroom，没有获得应用预测上下文。

E 组 `#11` 的编译消融检查结果：

```text
# CONFIG_PARP_RECLAIM_BIN_SCORE is not set
reclaim_bin_stats_exists=0
reclaim_bin_symbols_exist=0
```

## 逐轮核心数据

| 组别 | Seed | PageFault | Major | Refault | pgscan | Direct scan | memcg 次数 | memcg ms | OOM | Kill | Failure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A Native | 20260821 | 4,504,355 | 2,353 | 646,665 | 1,818,509 | 1,818,509 | 14,525 | 1,351.443 | 1 | 1 | 1 |
| A Native | 20260822 | 4,438,368 | 891 | 453,802 | 1,198,755 | 1,198,755 | 6,507 | 314.842 | 1 | 1 | 1 |
| A Native | 20260823 | 4,571,249 | 902 | 465,224 | 1,201,081 | 1,201,081 | 6,618 | 308.764 | 1 | 1 | 1 |
| B Full APPLY | 20260821 | 4,498,058 | 2,756 | 691,956 | 3,564,989 | 1,368,833 | 5,860 | 3,455.541 | 1 | 1 | 1 |
| B Full APPLY | 20260822 | 4,438,514 | 929 | 270,595 | 1,222,414 | 1,020 | 1,494 | 272.186 | 1 | 1 | 1 |
| B Full APPLY | 20260823 | 4,571,780 | 922 | 465,812 | 1,217,463 | 1,724 | 1,312 | 337.458 | 2 | 1 | 1 |
| C Effective only | 20260821 | 4,496,424 | 918 | 444,658 | 1,199,656 | 1,199,656 | 6,537 | 316.140 | 1 | 1 | 1 |
| C Effective only | 20260822 | 4,435,974 | 894 | 447,662 | 1,192,486 | 1,192,486 | 6,400 | 300.957 | 1 | 1 | 1 |
| C Effective only | 20260823 | 4,569,467 | 897 | 453,796 | 1,192,449 | 1,192,449 | 6,389 | 291.189 | 1 | 1 | 1 |
| D Tier2 + reclaim-bin | 20260821 | 4,492,899 | 896 | 428,073 | 1,196,986 | 196 | 1,408 | 240.817 | 1 | 1 | 1 |
| D Tier2 + reclaim-bin | 20260822 | 4,437,756 | 903 | 450,476 | 1,193,360 | 447 | 870 | 210.583 | 1 | 1 | 1 |
| D Tier2 + reclaim-bin | 20260823 | 4,567,788 | 899 | 450,895 | 1,196,788 | 720 | 1,414 | 379.616 | 3 | 1 | 1 |
| E Full no-reclaim | 20260821 | 4,506,010 | 2,997 | 1,035,738 | 7,725,430 | 3,740,023 | 4,989 | 9,295.545 | 2 | 1 | 1 |
| E Full no-reclaim | 20260822 | 4,447,948 | 943 | 276,586 | 1,215,236 | 972 | 1,224 | 242.428 | 1 | 1 | 1 |
| E Full no-reclaim | 20260823 | 4,575,453 | 891 | 448,724 | 1,195,085 | 851 | 1,214 | 309.697 | 1 | 1 | 1 |

## 限制与后续工作

1. effective-tier 启动时未预留每页元数据，虽然模式显示 APPLY，但没有产生评分和 tier 动作；此外模型来源仍为 `ENGINEERING_FIXTURE_UNTRAINED`。因此 B/C/E 不能用于评价 effective-tier 或真正 Full APPLY。
2. reclaim-bin 的 `context_hits=0`，说明应用预测分数没有进入当前 scorer；本轮验证的是 cgroup headroom 与层级继承路径。
3. 当前 OOM-THRESHOLD 场景有意把每轮推到至少一次 OOM kill，适合证明 OOM 指标可采集，但不适合区分“是否完全避免 OOM”。后续应增加临界压力 sweep，在多个压力档和更多 seed 下比较 OOM kill 概率、首次 OOM 阈值和达到 OOM 前的有效工作量。
4. 每组只有三个有效轮次，且采用固定 A→B→C→D→E 顺序、不清缓存，冷启动与热缓存混入了模块差异。下一轮必须做顺序交叉平衡，并分别报告 cold/warm 结果和置信区间。
5. 修复内核必须用 `parp_effective_tier_reserve=1` 启动，并以 `metadata_ready=1`、`scores>0`、实际升/降 tier 计数为有效性门槛。
6. 需要完成新的 LSAPP 对齐训练集和 checkpoint，并让在线服务把应用预测上下文写入内核，再重新测试 Full APPLY。

## 最终判定

| 项目 | 判定 |
|---|---|
| 同环境、同计划、同 seed 配对 | 通过 |
| reclaim-bin 层级继承与 scorer 生效 | 通过 |
| reclaim-bin 编译宏消融 | 通过 |
| direct reclaim 降低 | 通过诊断验证，但存在路径转移，需结合总扫描与耗时 |
| effective-tier 实际动作 | 未通过；启动元数据未预留，旧 B/C/E 无有效评分和 tier 动作 |
| PageFault 改善目标 | 未达到 |
| OOM kill 改善目标 | 未达到 |
| 失败峰值改善目标 | 未达到 |
| LSTM 在线收益验证 | 未完成，应用上下文未进入内核 |
| 完整 APPLY 总体验收 | 未形成有效 Full APPLY 样本；修复启动条件后重测 |

## 原始结果目录

- Native 主目录：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-native-20260826_102359-6.17.13-native-6.17.13`
- Native `20260822` 补跑：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-native-20260826_102923-6.17.13-native-6.17.13`
- Full APPLY 主目录：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-combined-20260826_112334-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`
- Full APPLY `20260823` 补跑：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-combined-20260826_112841-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`
- Effective only：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-effective-20260826_113058-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`
- Tier2 + reclaim-bin：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-tier2-20260826_113507-6.17.13-parp-lzx-v4.2-apply-reclaim-bin`
- Full no-reclaim 主目录：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-combined_no_reclaim-20260826_114743-6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin`
- Full no-reclaim `20260823` 补跑：`/home/lzx/Desktop/PARP/test/outputs/parp_oom_threshold/peak-full-combined_no_reclaim-20260826_115243-6.17.13-parp-lzx-v4.2-apply-no-reclaim-bin`
