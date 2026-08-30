# Native 与 r11 LSTM/bin/cold-aggressive 第四场景对比报告

日期：2026-08-30  
场景：`cold_dirty_preserve_hot_clean`  
结论状态：三组各 3 轮，最终 9 轮全部 `VALID`。<!-- lzx-note -->

## 一、结论

本场景已经证明：在训练集对齐且预测正确的应用切换序列下，**LSTM + reclaim-bin 能够把回收来源从预测热应用迁移到预测冷应用，并完整保留随后要复用的热应用干净冷页**。

相对 Linux 6.17.13 Native，`LSTM + bin` 的三轮均值为：

- 热应用 384 MiB 干净冷页的保留率由 **25.39% 提高到 100%**；压力阶段少回收热干净页 **286.52 MiB**。
- 预测冷应用占可测回收来源的比例由 **49.75% 提高到 95.43%**，提高 **45.68 个百分点**。
- 三个热应用首次精确复用合计的 `workingset_refault_file` 由 **95,061 页降到 0**。
- 首次复用 major fault 由 **3 次降到 0**，文件读取量由 **371.33 MiB 降到 0**。
- 三个热应用首次复用总耗时由 **243.95 ms 降到 17.75 ms**，降低 **92.72%**。
- 压力阶段 direct reclaim 扫描页数降低 **0.79%**；PSI full 总停顿增加 **3.87%**，压力写入量增加 **0.68%**。这两项小幅变化不能作为额外收益，但也没有抵消复用阶段的显著收益。

`LSTM + bin + cold-aggressive/workload-aware` 也能完整保护热干净页，但**没有比 bin-only 获得额外复用收益，反而显著增加压力阶段代价**：

- PSI full 总停顿从 bin-only 的 **63.05 ms** 增到 **629.44 ms**，增加约 **898.31%**。
- 压力写入量从 **595.62 MiB** 增到 **887.70 MiB**，增加 **49.04%**。
- direct reclaim 扫描从 **193,421 页**增到 **269,687 页**，增加 **39.43%**；扫描回收效率从约 **100%** 降到 **72.87%**。
- 新增 `pswpout` **74,859 页（约 292.42 MiB）**、`pswpin` **1,092 页（约 4.27 MiB）**和压力窗口 `pgmajfault` **995 次**；Native 和 bin-only 的这些指标均为 0。

因此，本场景的工程结论是：

1. **LSTM + reclaim-bin 机制验证通过，适合作为当前默认方案继续进入真实 PC 使用验证。**
2. **cold-aggressive/workload-aware 不应默认开启。**当前实现虽然动作真实发生，但在本场景中属于重复加压，成本明显高于收益。
3. 本结果证明的是训练序列对齐场景下的因果机制，不等同于所有真实桌面负载都能获得同等幅度的收益。

## 二、实验对象与消融边界

| 组别 | 内核 | 运行时策略 | effective-tier | Tier2 主动回收 | reclaim-bin | cold-aggressive / workload-aware |
|---|---|---|---:|---:|---:|---:|
| Native | `6.17.13-native-6.17.13` | `native_kernel` | 不存在/关闭 | 不存在/关闭 | 不存在/关闭 | 不存在/关闭 |
| LSTM + bin | `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin` | `bin_lstm` | 0 | 0 | 1 | 0 / 0 |
| LSTM + bin + cold-aggressive | 同一 r11 内核 | `bin_workload_lstm` | 0 | 0 | 1 | 1 / 1 |

两组 PARP 使用相同内核，仅通过运行时开关消融；没有启用 effective-tier、Tier2 主动回收和工作集水位线策略。

三个组均使用 seed `20260830`、`20260831`、`20260832`，应用、页面容量、切换顺序、压力大小和复用动作完全相同。所有中途被严格门禁判为 `INVALID` 的诊断轮次均未纳入本报告。

## 三、自动化场景

### 3.1 应用与预测序列

预测热应用：Firefox、Thunderbird、VLC。  
预测冷应用：GIMP、LibreOffice、Evince、ImageViewer、Solitaire。

最终 LSTM 五步历史固定为：

```text
Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC
```

有效轮次中预测结果稳定为：

- Firefox：Top-1，概率约 85.47%。
- Thunderbird：Top-2，概率约 9.27%。
- 五个冷应用的单应用概率均不高于 0.20%，满足不高于 1% 的门槛。

运行时服务会忽略不属于 LSAPP 词表的瞬态 `<UNKNOWN>` 窗口，避免其挤掉真实应用历史；原始 X11 事件仍保留在审计日志中。

### 3.2 页面构成

每个 fixture 使用三个不同文件/inode，以便用 `mincore()` 分别归因：

- `clean.data`：写入、flush、fsync 后执行 `MADV_COLD`。
- `dirty.data`：先写入并 fsync，再逐页修改但不 flush，随后执行 `MADV_COLD`。
- `hot.data`：小型控制区域，只在对应真实前台应用切换时触碰。

容量如下：

| 页面池 | 容量 |
|---|---:|
| 五个预测冷应用的干净冷页 | 480 MiB |
| 五个预测冷应用的脏冷页 | 400 MiB |
| 三个预测热应用的干净冷页 | 384 MiB |
| 三个预测热应用的脏页 | 192 MiB |
| 目标回收量 | 768 MiB |
| 压力分配量 | 1536 MiB |

容量满足：

```text
cold_clean(480) < target(768) <= cold_clean+cold_dirty(880)
cold_clean(480)+hot_clean(384) >= target(768)
```

这保证 Native 可以通过“冷应用干净页 + 热应用干净页”完成目标，而预测方案具备通过“冷应用干净页 + 冷应用脏页”替代热应用干净页的容量条件。

### 3.3 确定性脏页闭环

为了防止后台回写在线程调度波动下提前把实验脏页清洗掉，自动化执行：

1. 对全部应用执行 `PREPARE`。
2. 在最终两次训练序列切换前执行 `COLDIFY`。
3. 在最终 VLC 事件前，只对五个预测冷应用执行一次 `REDIRTY`。
4. 写入“画像已就绪”时间锚点。
5. 触发最终 VLC 前台事件、LSTM 推理和 `/dev/myfs` 原子下沉。
6. workload-aware 组严格要求五个冷 cgroup 均为有效 `FILE_DIRTY`，且 ABI v3、绑定和 profile 均成功。

该闭环不触碰三组热应用的 `clean.data`；它们只有在压力结束后的首次复用阶段才会被逐页读取。

### 3.4 压力后复用

压力稳定后依次切回 Firefox、Thunderbird、VLC，并对每个应用：

1. 采集复用前快照。
2. 逐页读取完整 128 MiB `clean.data`。
3. 采集首次复用后的 fault、refault、I/O、PSI 和耗时。
4. 再读取一次相同页面作为 warm control。

全部 warm control 的 refault 和 major fault 均为 0，说明首次复用指标来自真实页面重新装入，而不是采集器自身噪声。

## 四、三轮原始主指标

### 4.1 Native

| Seed | 冷来源占比 | 热干净页保留率 | 回收热干净页 | 回收冷脏页 | 首次 file refault | 首次 major fault | 首次复用总耗时 | 压力 PSI full | 压力写入 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20260830 | 44.21% | 27.46% | 278.56 MiB | 1.75 MiB | 88,576 | 3 | 189.01 ms | 63.25 ms | 592.74 MiB |
| 20260831 | 52.46% | 25.00% | 288.00 MiB | 2.88 MiB | 98,304 | 3 | 257.53 ms | 53.41 ms | 595.00 MiB |
| 20260832 | 52.58% | 23.70% | 293.00 MiB | 3.05 MiB | 98,304 | 3 | 285.32 ms | 65.44 ms | 587.00 MiB |

### 4.2 LSTM + reclaim-bin

| Seed | 冷来源占比 | 热干净页保留率 | 回收热干净页 | 回收冷脏页 | 首次 file refault | 首次 major fault | 首次复用总耗时 | 压力 PSI full | 压力写入 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20260830 | 95.44% | 100% | 0 | 320.00 MiB | 0 | 0 | 18.17 ms | 70.53 ms | 595.95 MiB |
| 20260831 | 95.43% | 100% | 0 | 320.00 MiB | 0 | 0 | 20.53 ms | 51.20 ms | 595.81 MiB |
| 20260832 | 95.42% | 100% | 0 | 320.00 MiB | 0 | 0 | 14.56 ms | 67.42 ms | 595.11 MiB |

### 4.3 LSTM + reclaim-bin + cold-aggressive/workload-aware

| Seed | 冷来源占比 | 热干净页保留率 | 回收热干净页 | 回收冷脏页 | 首次 file refault | 首次 major fault | 首次复用总耗时 | 压力 PSI full | 压力写入 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20260830 | 92.89% | 100% | 0 | 160.00 MiB | 0 | 0 | 14.75 ms | 732.76 ms | 908.17 MiB |
| 20260831 | 93.19% | 100% | 0 | 160.00 MiB | 0 | 0 | 34.46 ms | 696.41 ms | 886.38 MiB |
| 20260832 | 93.43% | 100% | 0 | 166.75 MiB | 0 | 0 | 17.91 ms | 459.13 ms | 868.54 MiB |

## 五、三轮均值对比

| 指标 | Native | LSTM + bin | LSTM + bin + cold-aggressive |
|---|---:|---:|---:|
| 冷应用干净区域回收 | 322.42 MiB | 397.74 MiB | 274.67 MiB |
| 冷应用脏区域回收 | 2.56 MiB | 320.00 MiB | 162.25 MiB |
| 热应用干净区域回收 | 286.52 MiB | 0 | 0 |
| 热应用脏区域回收 | 39.04 MiB | 0 | 0 |
| 热干净页保留率 | 25.39% | 100% | 100% |
| 冷来源占比 | 49.75% | 95.43% | 93.17% |
| 首次复用 file refault | 95,061 页 | 0 | 0 |
| 首次复用 pgfault | 2,046 | 0 | 0 |
| 首次复用 pgmajfault | 3 | 0 | 0 |
| 首次复用文件读取 | 371.33 MiB | 0 | 0 |
| 首次复用 PSI full | 101.98 ms | 0 | 0 |
| 首次复用总耗时 | 243.95 ms | 17.75 ms | 22.38 ms |
| 压力 direct pgscan | 194,966 页 | 193,421 页 | 269,687 页 |
| 压力 pgsteal | 194,207 页 | 193,420 页 | 196,464 页 |
| 扫描回收效率 | 99.61% | 约 100% | 72.87% |
| 压力 PSI some | 67.62 ms | 68.13 ms | 646.52 ms |
| 压力 PSI full | 60.70 ms | 63.05 ms | 629.44 ms |
| 压力写入 | 591.58 MiB | 595.62 MiB | 887.70 MiB |
| 压力 `pswpout` | 0 | 0 | 74,859 页 |
| 压力 `pswpin` | 0 | 0 | 1,092 页 |
| 压力 `pgmajfault` | 0 | 0 | 995 |

`workingset_refault_file` 的单位是页。Native 的 95,061 页约对应 371.33 MiB，与首次复用实际文件读取量一致。

## 六、回收来源分布

三轮平均的 fixture 页面来源如下。

### Native

| 应用 | 干净区域回收 | 脏区域回收 |
|---|---:|---:|
| Firefox（热） | 128.00 MiB | 37.68 MiB |
| Thunderbird（热） | 127.33 MiB | 1.35 MiB |
| VLC（热） | 31.19 MiB | 0 |
| GIMP（冷） | 58.42 MiB | 0 |
| LibreOffice（冷） | 14.00 MiB | 0 |
| Evince（冷） | 84.00 MiB | 0.08 MiB |
| ImageViewer（冷） | 96.00 MiB | 2.48 MiB |
| Solitaire（冷） | 70.00 MiB | 0 |

Native 基本沿着全局干净页优先路径回收，冷脏区域三轮平均仅回收 2.56 MiB，因此需要牺牲约 286.52 MiB 随后会复用的热应用干净冷页。

### LSTM + bin

| 应用 | 干净区域回收 | 脏区域回收 |
|---|---:|---:|
| Firefox / Thunderbird / VLC（热） | 0 | 0 |
| GIMP（冷） | 96.00 MiB | 80.00 MiB |
| LibreOffice（冷） | 96.00 MiB | 80.00 MiB |
| Evince（冷） | 96.00 MiB | 80.00 MiB |
| ImageViewer（冷） | 96.00 MiB | 80.00 MiB |
| Solitaire（冷） | 13.74 MiB | 0 |

bin 顺序先扫描预测冷 cgroup。前四个冷应用的干净和脏区域被完整回收，再从第五个冷应用补足缺口；压力在到达热应用前已经满足，因此三组热应用的测量区域全部保留。

### cold-aggressive/workload-aware

| 应用 | 干净区域回收 | 脏区域回收 |
|---|---:|---:|
| Firefox / Thunderbird / VLC（热） | 0 | 0 |
| GIMP（冷） | 96.00 MiB | 80.00 MiB |
| LibreOffice（冷） | 96.00 MiB | 80.00 MiB |
| Evince（冷） | 82.67 MiB | 2.25 MiB |
| ImageViewer（冷） | 0 | 0 |
| Solitaire（冷） | 0 | 0 |

增强策略在最先访问的少数冷 cgroup 内进行更深扫描，同时回收这些应用的其他 GUI/匿名内存，因此在到达 ImageViewer、Solitaire 之前已经满足总回收目标。它仍然保护了热页，但没有按预期进一步增加可测冷脏区域的回收；相反，可测冷脏区域比 bin-only 少 49.30%。

## 七、内核动作与 cold-aggressive 无额外收益的原因

| 内核计数（三轮均值） | LSTM + bin | cold-aggressive/workload-aware |
|---|---:|---:|
| `reclaim-bin policy_hits` | 32,630 | 9,571 |
| `subtree_selected` | 4,384 | 2,301 |
| cold passes | 0 | 2,301 |
| workload profile hits | 0 | 2,301 |
| workload profile misses | 0 | 0 |
| `file_dirty_passes` | 0 | 2,297 |
| cold scanned | 0 | 271,732 页 |
| cold reclaimed | 0 | 196,685 页（约 768.30 MiB） |
| `writepage_promotions` | 0 | 0 |

这些计数证明 workload-aware 路径真实执行，并非开关未生效；但 `writepage_promotions=0` 说明本轮 direct reclaim 的 `sc->may_writepage` 进入 cold-aggressive 前已经为 1。内核只在 `allow_writepage && !sc->may_writepage` 时记一次 promotion，所以当前场景没有发生“原先禁止写回、PARP 在第一轮提前开放写回”的状态转换。

实际发生的是：

1. LSTM + bin 已经把预测冷应用放到最先扫描的位置，并且冷池容量足以在访问热应用前完成回收。
2. cold-aggressive 再将冷 cgroup 的 reclaim priority 下调（增加扫描强度），FILE_DIRTY profile 同时下沉 `swappiness=20`。
3. 更深扫描触及更多匿名页，产生约 292.42 MiB swap-out、约 4.27 MiB swap-in和约 995 次压力窗口 major fault。
4. 扫描量增加而 `pgsteal` 基本不变，效率下降；I/O 和 PSI 明显上升。
5. 因为前几个冷 cgroup 已提供足够的其他可回收页，内核更早停止，反而没有继续扫描后两个冷应用的目标脏区域。

所以增强组效果差的核心不是 LSTM 或 `/dev/myfs` 失效，而是：**在 bin-only 已能避开热应用的情况下，额外提高冷 cgroup 扫描压力没有新的保护对象，只增加了深扫描和换页成本。**

## 八、有效性与限制

本轮有效性门禁包括：

- 三组均为相同 8 个 GUI 应用、相同训练历史、相同 seed。
- 压力前 clean/dirty/hot 三类文件均至少 95% 驻留。
- `memory.stat file_dirty` 至少达到配置脏区域的 80%。
- PARP 组的 LSTM 排名、概率和 8 个应用绑定均满足门槛。
- workload-aware 组要求 `/dev/myfs` ABI v3、五个冷应用 `FILE_DIRTY` profile 有效、内核 profile miss 为 0。
- 压力分配达到 1536 MiB，没有 OOM 或安全停止。
- 三个热应用均完成 128 MiB 精确首次读取和 warm control。

限制：

1. 这是为验证机制构造的受控页面池；它证明页面回收方向和后续复用收益，但不能替代长时间真实桌面工作流。
2. 三轮样本可以证明结果在本机上重复，但不足以给出跨机器统计置信区间。
3. 本场景主要验证文件页 refault；没有让热应用匿名页成为复用目标，因此不能用本报告单独证明匿名页 swap-in 收益。
4. `mincore()` 证明某 inode 的页面是否离开驻留集，但页面从 dirty 到 writeback 再到 clean/evicted 的细粒度时序仍需 tracepoint 才能逐页重建。

## 九、建议

1. 当前默认进入真实 PC 阶段时使用 **LSTM + reclaim-bin**，保持 cold-aggressive/workload-aware 关闭。
2. 若继续改进 cold-aggressive，应增加收益门槛：只有预测冷干净页不足以覆盖回收缺口、且热应用页面确实面临被回收时才允许额外深扫。
3. 对 cold-aggressive 增加 I/O、PSI、swap-out 预算；达到预算后退回 bin-only，而不是持续 priority boost。
4. 区分 `sc->may_writepage` 原本为 0 和原本为 1 的路径。只有前者才能验证“提前开放脏页写回”，后者不应把 `allow_writepage` 作为收益来源。
5. 下一阶段真实使用验证继续采集：前台切换延迟、前台 cgroup PSI、file/anon refault、swap-in、major fault，并使用同一事件序列进行 Native/bin-only 配对。

## 十、原始数据与实现

- Native 三轮：[summary.json](/home/lzx/Desktop/PARP/test/outputs/cold_dirty_preserve_hot_clean/native_kernel-20260830_135037-6.17.13-native-6.17.13/summary.json)
- LSTM + bin 三轮：[summary.json](/home/lzx/Desktop/PARP/test/outputs/cold_dirty_preserve_hot_clean/bin_lstm-20260830_133939-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin/summary.json)
- cold-aggressive/workload-aware 三轮：[summary.json](/home/lzx/Desktop/PARP/test/outputs/cold_dirty_preserve_hot_clean/bin_workload_lstm-20260830_133611-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin/summary.json)
- 场景配置：[parp-cold-dirty-preserve-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-cold-dirty-preserve-config-lzx.json)
- 页面 fixture：[reclaim-substitution-fixture-lzx.py](/home/lzx/Desktop/PARP/test/test/reclaim-substitution-fixture-lzx.py)
- 实验 runner：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)
- workload 分类器：[reclaim_workload.py](/home/lzx/Desktop/PARP/lzx/service/runtime_monitor/core/reclaim_workload.py)
- 内核 cold-aggressive 入口：[vmscan.c](/home/lzx/Desktop/PARP/lzx/kernel/src/linux-6.17.13-parp-lzx/mm/vmscan.c)

