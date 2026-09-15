# R1-R5 真实应用自动化：Linux 6.17.13 Native 与 PARP R11 配对实验报告

日期：2026-09-02

## 1. 结论

真实 GUI 应用操作可以替代原先的大部分主动申请/触碰内存操作，并且能够复现 PARP 的核心收益链：

1. LSTM 在固定训练集序列 `Thunderbird → Firefox → Thunderbird → Firefox → VLC` 下，稳定预测 Firefox 为 Top-1（约 0.777）、Thunderbird 为 Top-2（约 0.126），五个冷应用概率低于冷阈值。
2. 预测结果通过 `/dev/myfs` ABI v3 下沉，8 个应用 cgroup 均无歧义绑定。
3. bin+LSTM 把约 256 MiB 回收量更多地转向预测冷应用，并减少预测热应用被回收的内存。
4. 在包含真实热应用再次操作的 R2-R5 中，major fault、refault 和 swap-in 的三轮中位数总体下降；R3/R4 的交互动作总延迟也下降。
5. 新版 R5 加入 workload-aware cold pressure 后，效果显著增强：冷应用回收占比由 71.66% 提高到 96.49%，热应用回收量由 73.90 MiB 降到 9.21 MiB；热应用复用期 major fault 由 1398 降到 1、refault 由 1892 降到 1、swap-in 由 1818 降到 1。

但必须同时保留三个限制：

- 新版 R5 的交互动作总延迟中位数由 4761 ms 增至 4940 ms（+3.75%），说明 fault/refault 改善尚未稳定转化为端到端动作延迟收益。
- 内存 PSI 的三轮中位数双方均为 0，本实验不能证明 PSI/卡顿改善。
- 真实 GIMP/LibreOffice 稳态没有形成可重复的 `FILE_DIRTY` 工作集，`writepage_promotions=0`。因此真实应用版 R5 验证了 workload-aware 匿名页/干净文件页选择，不能替代原受控 fixture 对 `FILE_DIRTY + may_writepage` 的 cold-aggressive 验证。

综合判断：R1-R4 的应用原生机制验证可以算通过；新版 R5 的 workload-aware 页面类型与冷热应用回收验证也通过，但脏文件页 cold-aggressive 子机制仍只能由受控场景验证。当前结果足以进入真实 PC 长时使用评估，但不能把三轮实验写成统计显著或完整用户体验结论。

## 2. 实验对象与公平性

### 2.1 内核与策略

- Native：`6.17.13-native-6.17.13`
- PARP：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 主五场景对比：Native `native_kernel` vs PARP `bin_lstm`
- 新版 R5 对比：Native `native_kernel` vs PARP `bin_workload_lstm`

### 2.2 工作负载

- 热应用：Firefox（Epiphany）、Thunderbird、VLC。
- 冷应用：GIMP、LibreOffice、Evince、Image Viewer、Solitaire。
- 工作集全部由真实应用操作形成：浏览与滚动网页、邮件滚动、音频播放/跳转、加载和编辑 4096×4096 图片、编辑文档、翻页、缩放图片、纸牌操作。
- 不使用应用工作集匿名页 fixture 或独立压力分配器。
- 内存回收只向上述应用的父 cgroup 写入约 256 MiB `memory.reclaim`。

### 2.3 严格配对

主实验 Native 与 PARP 均为 R1-R5 × 3，共 15 对；PARP 使用 Native 输出目录作为 `--replay-from`。每对实验使用相同应用、离线资产、动作、种子和动作计划 SHA-256。

新版 R5 使用种子 `20260911`、`20260912`、`20260913`，三对动作计划 SHA-256 分别为：

- `97c17421ef341a1c1cba18a3ca5da03115c324f0b1cc45fceb6524a87b719016`
- `7097405ae4e4949b02b6913ac6c0406ada4999d62c04facc66175b0b9cf00d92`
- `e43f959097c3759a46db3622eee3265f92a7d65d0ac1d139cf1c53d883c14a6b`

三对均逐字节匹配，Native 3/3 `VALID`，PARP 3/3 `VALID`。runtime service 全程 `NRestarts=0`，没有 OOM 或安全线中止。

## 3. 主 bin+LSTM 五场景结果

下表均为三轮中位数。百分比为 PARP 相对 Native 的变化；“冷占比”为回收量中来自预测冷应用的比例。

| 场景 | 冷占比 Native → PARP | 热应用回收 MiB | 热应用复用 pgfault | major fault | refault | swap-in | 交互动作总延迟 |
|---|---:|---:|---:|---:|---:|---:|---:|
| R1 冷应用退出活跃集 | 72.94% → 81.62%（+8.68 pp） | 70.13 → 47.94（-31.63%） | N.A. | N.A. | N.A. | N.A. | N.A. |
| R2 预测即将返回 | 70.52% → 79.35%（+8.83 pp） | 76.49 → 53.84（-29.62%） | 1891 → 1599（-15.44%） | 1615 → 1232（-23.72%） | 1966 → 1391（-29.25%） | 1964 → 1391（-29.18%） | 3310 → 3261 ms（-1.47%） |
| R3 多热/多冷来源分布 | 72.19% → 79.60%（+7.41 pp） | 72.39 → 53.04（-26.74%） | 7494 → 7877（+5.11%） | 2148 → 2012（-6.33%） | 2831 → 2302（-18.69%） | 2826 → 2299（-18.65%） | 5691 → 4677 ms（-17.81%） |
| R4 高代价冷页替代 | 67.52% → 78.85%（+11.33 pp） | 84.42 → 55.09（-34.75%） | 7499 → 7932（+5.77%） | 2474 → 1772（-28.38%） | 3078 → 1942（-36.91%） | 3056 → 1938（-36.58%） | 5751 → 4720 ms（-17.92%） |
| R5 原 bin-only 应用版 | 74.28% → 80.98%（+6.70 pp） | 67.03 → 49.58（-26.04%） | 7986 → 8316（+4.13%） | 2239 → 1664（-25.68%） | 2832 → 1962（-30.72%） | 2820 → 1960（-30.50%） | 4761 → 5715 ms（+20.03%） |

### 3.1 能说明什么

- 五个场景的三轮中位数均表现为更高的冷应用回收占比、更低的热应用回收量。
- R2-R5 的 major fault、refault、swap-in 中位数全部下降，说明“少回收预测热应用”已经转化成实际复用成本下降。
- R3/R4 的总 pgfault 小幅增加但 major fault/refault/swap-in 下降。这说明新增的多为低代价 minor fault；不能只看总 pgfault 判断机制好坏。
- 原 bin-only R5 虽降低 refault/swap-in，但延迟增加 20.03%，说明只有应用间排序还不足以稳定改善 R5 的端到端体验。

### 3.2 稳定性限制

主实验每个场景只有三对数据。回收来源的方向最稳定；复用 fault 与延迟存在轮间波动。主 R2-R5 的 refault 改善方向通常为 2/3 轮，而非每轮都改善，因此适合作为机制证据，不应写成统计显著结论。

## 4. 新版 R5 workload-aware 配对结果

新版 R5 保留同一应用与约 256 MiB 回收目标，但允许 runtime service 根据每轮 cgroup `memory.stat` 实际组成下沉有效 workload profile。正式三轮均观测到 GIMP=`FILE_CLEAN`、LibreOffice=`ANON_HEAVY`；最终完整绑定集为 6 个 `ANON_HEAVY`、1 个 `FILE_CLEAN`、1 个 `MIXED`。

| 指标（三轮中位数） | Native | PARP workload-aware | 变化 |
|---|---:|---:|---:|
| 实际回收量 | 259.94 MiB | 262.97 MiB | +1.17% |
| 冷应用回收占比 | 71.66% | 96.49% | +24.83 pp |
| 热应用回收量 | 73.90 MiB | 9.21 MiB | -87.54% |
| 冷应用回收量 | 186.87 MiB | 253.48 MiB | +35.65% |
| 回收期 pgscan | 81,158 | 83,325 | +2.67% |
| 回收期 pgfault | 54 | 18 | -66.67% |
| 回收期 major fault | 1 | 0 | -100% |
| 回收期 refault | 24 | 0 | -100% |
| 回收期 swap-out | 10,802 | 3,070 | -71.58% |
| 热应用复用 pgfault | 8,024 | 6,097 | -24.02% |
| 热应用复用 major fault | 1,398 | 1 | -99.93% |
| 热应用复用 refault | 1,892 | 1 | -99.95% |
| 热应用复用 swap-in | 1,818 | 1 | -99.94% |
| 交互动作总延迟 | 4,761 ms | 4,940 ms | +3.75% |
| 复用期 PSI some/full | 0 / 0 μs | 0 / 0 μs | 无可比信号 |

### 4.1 逐轮方向

- 冷应用回收占比：PARP 3/3 更高（+33.19、+23.34、+26.65 pp）。
- 热应用回收量：PARP 3/3 更低（-85.77、-60.56、-69.47 MiB）。
- 热应用 major fault/refault：PARP 3/3 更低。
- 热应用总 pgfault：PARP 2/3 更低。
- 交互动作总延迟：PARP 1/3 更低、2/3 更高。

### 4.2 回收来源

各应用三轮回收量中位数（MiB）：

| 应用 | 类型 | Native | PARP |
|---|---|---:|---:|
| Firefox | 热 | 39.96 | 0.98 |
| Thunderbird | 热 | 29.00 | 7.95 |
| VLC | 热 | 7.49 | 0.09 |
| GIMP | 冷 | 145.65 | 241.20 |
| LibreOffice | 冷 | 33.71 | 6.32 |
| Evince | 冷 | 1.47 | 1.00 |
| Image Viewer | 冷 | 6.04 | 6.19 |
| Solitaire | 冷 | 0.99 | 0.51 |

主要变化是 PARP 大幅增加对预测冷 GIMP 工作集的回收，从而避免回收 Firefox/Thunderbird/VLC。该结果符合本场景的目标，但也说明当前策略会优先集中回收一个容量最大的低概率应用，需要在长时真实使用中继续观察公平性和错误预测代价。

### 4.3 内核动作证据

三轮 workload-aware cold pass 为 135、127、141 次，全部具有有效 profile：

- `ANON_HEAVY` pass：74、69、80。
- `FILE_CLEAN` pass：36、35、36。
- `MIXED` pass：25、23、25。
- `FILE_DIRTY` pass：0、0、0。
- `writepage_promotions`：0、0、0。

bin 子树选择同样生效：`subtree_selected` 中位数 251、`subtree_skipped` 中位数 1735。`rebin_moves` 两轮为 0、一轮为 17；本版主要收益来自扫描时动态子树选择和 workload-aware pressure，而不是必须移动静态 bin。

第三轮 `workload_profile_misses=4121`，前两轮为 48、46。内核在评估未绑定或无有效 profile 的 memcg 时会 fail-closed 并增加 miss；这些 miss 没有执行 workload 动作，但当前计数没有区分“未绑定 cgroup”和“已绑定 profile 失效”。后续应拆分计数，否则 miss 数不能直接当成预测错误率。

## 5. 为什么 fault 大幅下降但延迟没有同步下降

1. 自动化延迟是多个窗口切换、键盘动作和固定输入间隔的总和，不只包含缺页等待。
2. PARP 的回收期 pgscan 增加 2.67%，动态打分和 workload 分类也有少量 CPU 开销。
3. 本轮复用时的 PSI 中位数本来就是 0，说明当前 32 GiB 环境和 256 MiB 定向回收没有形成持续可测的 stall；major fault 下降无法从 PSI 再获得同量级改善。
4. 三轮样本过少，约 150–274 ms 的动作总延迟增加可能落在 GUI 调度抖动范围内。

因此当前最可靠的交付指标是回收来源、major fault、refault 和 swap-in；交互延迟应在真实 PC 长时场景中使用更细的“前台窗口恢复到首帧/可交互”指标重新测量。

## 6. R5 的适用边界

真实应用自动化曾尝试让 GIMP 形成大量脏文件页：加载多张 4096×4096 图片，使用 64 MiB tile cache，并通过 GIMP 自身的 Search Actions 对各画布执行 Invert。结果表明：

- GIMP 的 GEGL swap 文件确实可达数百 MiB；
- 图片仍在异步导入时可能短暂出现大量 `file_dirty`；
- 图片完全载入和编辑完成后，GEGL 会自行写净，稳定 profile 是 `FILE_CLEAN` 或按驻留比例成为 `ANON_HEAVY`；
- LibreOffice GUI 编辑主要增加匿名页，保存后没有稳定的大量脏文件页。

所以不能为了让 `writepage_promotions>0` 而把瞬时状态冒充成可重复用户场景。原第五场景要求的“第一轮直接回收冷应用脏文件页、保留热应用干净页”仍需要受控 fixture 精确构造页面状态；真实 GUI 场景负责验证更高层的应用冷热选择和匿名/文件 workload 自适应。

## 7. 数据位置

- 主 Native 15 轮：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260901_195238-6.17.13-native-6.17.13`
- 主 PARP bin+LSTM 15 轮：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_lstm-20260901_205141-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 新版 R5 Native 三轮：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_100838-6.17.13-native-6.17.13`
- 新版 R5 PARP workload-aware 三轮：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_workload_lstm-20260902_102537-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 过程与排除轮次记录：`/home/lzx/Desktop/PARP/test_reports/r1-r5-real-interaction-paired-progress-20260831-lzx.md`

所有失败校准和旧固定类别诊断目录均未纳入本报告统计。
