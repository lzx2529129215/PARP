# Linux Native 与 r11 LSTM reclaim 消融结果（2026-08-30）

## 结论

本轮 **机制验证通过**，但 **尚不能宣布 workload-aware 策略带来端到端用户性能收益**。

- `bin_lstm` 已稳定把回收来源从预测热应用转移到预测冷应用：冷应用回收来源从 Native 的平均 **81.00%** 提升到 **99.35%**，热应用内存回收从 **325.38 MiB** 降到 **12.54 MiB**。
- `bin_workload_lstm` 的 V3 画像、低概率 bin-0 选择和四类页面策略均实际执行；热应用回收进一步降至 **0.81 MiB**，冷应用来源为 **99.96%**。
- 不过 workload-aware 相比 `bin_lstm` 增加了匿名页换入/换出与 direct reclaim；PSI 也没有改善。因此它目前只能作为“页面类型策略已执行”的证据，不能作为默认性能优化交付。
- 当前 `workload_matrix_reclaim` 主要验证“谁被回收、回收什么页面”，压力解除后没有对被逐出的 fixture 做确定性前台重访；所以不能用本轮全局 `pgfault` 或压力期 refault 直接声称真实用户返回应用时的体验已提升。

## 实验设计与有效性

| 项目 | 设置 |
|---|---|
| Native 基线 | 独立内核 `6.17.13-native-6.17.13` |
| Apply 内核 | `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin` |
| 组别 | Linux Native、`bin_lstm`、`bin_workload_lstm` |
| 有效轮次 | 每组 3/3 VALID；种子 `20260830`、`20260831`、`20260832` |
| 固定 LSTM 历史 | `Thunderbird → Firefox → Thunderbird → Firefox → VLC` |
| LSTM 结果 | Firefox Top-1 ≈85.5%，Thunderbird Top-2 ≈9.3%；五个回收候选均低于 1% |
| 应用 | 热：Firefox、Thunderbird、VLC；冷：GIMP、ImageViewer、Evince、LibreOffice、Solitaire |
| fixture 冷池 / 压力 / 回收目标 | 2048 MiB / 2432 MiB / 1920 MiB |
| Native service 公平性 | 同一常驻服务继续监听、形成画像和执行 LSTM；仅禁用不存在的 `/dev/myfs` sink |
| Apply service 链路 | 3/3 prediction gate、ABI-v3 下沉、5 个冷 cgroup 画像绑定全部通过 |

三组的配置、应用顺序、GUI 操作、fixture、cgroup 边界和种子相同。Native 与 Apply 必须通过重启切换内核，故这是“相同设置、相同种子”的配对，而不是同一开机周期内随机交错的 A/B。

原始结果目录：

- Native：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/native_kernel-20260830_120702-6.17.13-native-6.17.13`
- bin-only：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/bin_lstm-20260830_115639-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- workload-aware：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/bin_workload_lstm-20260830_120040-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`

## 三轮汇总

数值为平均值 `[中位数]`。样本仅 3 轮且 PSI 存在明显波动，因此对 fault/PSI 以中位数和逐轮结果为主，不以百分比均值作交付结论。

| 指标 | Linux Native | Bin + LSTM | Bin + LSTM + workload |
|---|---:|---:|---:|
| 冷应用回收来源占比 | 81.00% [80.59] | 99.35% [99.33] | **99.96% [99.95]** |
| 热应用内存回收 | 325.38 MiB [322.31] | 12.54 MiB [13.02] | **0.81 MiB [0.88]** |
| 热 fixture 精确逐出 | 11.00 MiB [1.51] | **0 MiB** | **0 MiB** |
| 冷 fixture 精确逐出 | 974.00 MiB [1024.00] | 1131.97 MiB [1132.00] | 991.11 MiB [991.01] |
| `pgfault` | 629469 [626721] | 626550 [626633] | 626666 [626593] |
| `pgmajfault` | 4585 [2152] | **1463 [1478]** | 1949 [1918] |
| `workingset_refault_file` | 3523 [1] | 0 [0] | 0 [0] |
| `workingset_refault_anon` | 5016 [2461] | **1539 [1547]** | 2216 [2047] |
| `pswpin` | 5007 [2454] | **1538 [1546]** | 2213 [2045] |
| `pswpout` | 167399 [154584] | **121140 [120974]** | 260770 [260850] |
| `pgscan_direct` | 660931 [640726] | **609438 [609510]** | 736579 [736525] |
| PSI some | 1615.05 ms [1495.53] | 4221.76 ms [1546.35] | 1932.60 ms [1756.54] |
| PSI full | 1590.59 ms [1468.16] | 3786.71 ms [1503.37] | 1900.56 ms [1721.90] |

PSI full 的逐轮值（ms）为：Native `648 / 1468 / 2656`，Bin+LSTM `1503 / 8736 / 1121`，workload-aware `1661 / 2319 / 1722`。Bin+LSTM 的第二轮是明显离群高值；workload-aware 虽降低该离群值，但其中位数仍高于 Native 和 bin-only。

## 机制证据

### 1. LSTM 到内核的选择链路

在 workload-aware 三轮中：

- prediction gate 3/3 通过，`/dev/myfs` ABI-v3 写入成功；
- 五个目标 cgroup 都有有效画像，`workload_profile_misses=0`；
- 预测冷且画像有效的应用均进入 bin 0；每轮 bin-0 score 约 `6.7k` 次；
- 预测热应用位于较高 bin（5–7），未被 fixture 逐出；
- 平均回收页面 `492756` 页，平均扫描 `754431` 页。

这证明 r11 的“低概率阈值优先于 ordinal-rank 地板”已消除 ImageViewer 排名第七却无法进入早期回收 bin 的问题。

### 2. workload-aware 的页面类型策略

三轮平均实际 pass：ANON_HEAVY **3338**、FILE_CLEAN **500**、FILE_DIRTY **907**、MIXED **1993**；四类均非零。

| 画像 | 精确 fixture 逐出表现（3 轮一致趋势） | 解释 |
|---|---|---|
| ANON_HEAVY / GIMP | 约 256 MiB anon，主类型占比约 89% | 高 swappiness 倾向实际作用于匿名冷页 |
| FILE_CLEAN / LibreOffice、Solitaire | 约 78 MiB file、32 MiB anon，file 占约 71% | 保持偏文件页，而非强制只回收文件页 |
| FILE_DIRTY / Evince | 320 MiB file、0 anon，file 占 100% | 受控脏文件 fixture 被实际逐出 |
| MIXED / ImageViewer | 160 MiB file、约 114 MiB anon | 中性画像会同时处理两类页 |

`writepage_promotions=0` 的含义是扫描控制在这些 direct-reclaim pass 中本来就允许 writeback；FILE_DIRTY profile 已被绑定、被命中并产生文件页逐出，但没有出现“从禁止改为允许”的额外 promotion。因此还不能单独量化 `allow_writepage` 这一微策略的边际收益。

## 对指标的解读

1. **bin-only 是当前最稳的可保留结果。** 它在三轮都几乎不回收热应用，并在中位数上同时降低 major fault、匿名 refault、swap-in 与 direct scan。
2. **workload-aware 的选择更集中，但压力更激进。** 相对 bin-only，它把热应用回收从约 12.5 MiB 再降到 0.8 MiB，却使匿名 refault、swap-in、swap-out、direct scan 和 PSI 中位数上升。当前参数不应合入默认 Apply。
3. **总 `pgfault` 对本场景不敏感。** 三组均约 626k，主要来自固定 fixture 建立与压力分配；它不是压力解除后用户重访的 fault 指标。
4. **Native 的 file-refault 与 major-fault 均有单轮大值。** n=3 不足以把均值差异写成确定百分比收益；后续应扩大重复数并报告置信区间或至少分位数。

## 下一步（性能验证，而非再次验证机制）

保留当前 workload-matrix 作为“选择与页面类型动作”证据；另建一个 post-pressure reuse 场景：

1. 压力阶段只需回收预测冷应用的冷页即可满足目标；热应用 fixture 不应被逐出。
2. 解除压力后，以固定 GUI 前台序列切回预测热应用，并由 fixture 显式 `TOUCH_FILE`、`TOUCH_ANON` 多次读取其工作集。
3. 单独采集该重访窗口的 `pgfault`、`pgmajfault`、`workingset_refault_*`、`pswpin`、每次触摸延迟和 PSI；不再混入 fixture 初始化与压力分配的 fault。
4. 先比较 Native 与 `bin_lstm`；workload-aware 仅在其 PSI / swap-in 不劣于 bin-only 后再进入该性能比较。

在这一新场景验证前，交付表述应为：“LSTM bin-reclaim 已证实能将回收来源从热应用转向预测冷应用”；不应表述为“已经证明降低真实 PC 缺页和卡顿”。
