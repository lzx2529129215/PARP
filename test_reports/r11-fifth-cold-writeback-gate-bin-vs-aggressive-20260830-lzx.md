# r11 第五场景：bin-only 与 cold-aggressive 配对验证报告

日期：2026-08-30  
场景：`cold_writeback_gate_hot_reuse`  
内核：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`  
结论状态：bin-only 与 cold-aggressive 各 3 轮，正式 6 轮全部 `VALID`。<!-- lzx-note -->

## 一、结论

第五场景证明：**当预测冷应用的干净页不足、冷脏页足够、原生 reclaim 初始不允许 writepage、且热应用页面确实可能被牺牲时，cold-aggressive 能在现有 LSTM + reclaim-bin 之上获得增量收益。**

相对 bin-only，cold-aggressive 三轮均值为：

- 压力前属于冷脏区域、压力后已离开驻留集的页面由 **323.01 MiB 增至 359.63 MiB**，增加 **11.33%**。
- 热应用干净冷页回收由 **25.49 MiB 降至 0**，热干净页保留率由 **95.57% 提高到 100%**。
- 热应用首次复用 `workingset_refault_file` 由 **13,971 页降到 0**，major fault 由 **1 次降到 0**，文件读取量由 **54.58 MiB 降到 0**。
- 三个热应用首次精确复用合计耗时由 **53.27 ms 降到 20.91 ms**，降低 **60.75%**。
- 压力阶段 PSI full 由 **55.79 ms 降到 31.87 ms**，降低 **42.88%**；写入量由 **732.89 MiB 降到 715.23 MiB**，降低 **2.41%**。
- `pgscan` 由 **130,066 页增到 142,109 页**，增加 **9.26%**；平均每轮扫描回收效率由约 **100% 降至 92.69%**。这是增强策略的主要代价。
- 两组的 `pswpout`、`pswpin`、压力窗口 major fault 和 OOM 均为 0，没有重现第四场景的匿名换出问题。

增强组三轮分别产生 **1,742、2,125、1,969 次 `writepage_promotions`**，并且正式轮次 `workload_profile_misses=0`。因此本次收益不是开关存在但没有动作，也不是无画像 cgroup 冒充策略命中。

工程结论：

1. cold-aggressive 不是普遍无效；它在第五场景的受限条件下有明确收益。
2. cold-aggressive 仍不适合无条件默认开启。第四场景中 bin-only 已足够时，它会增加 swap 和 PSI；第五场景中只有存在冷脏页缺口和热页风险时才有收益。
3. 推荐策略是 **bin-only 默认开启，cold-aggressive 由缺口/风险门控按需开启**。

## 二、第五场景如何满足触发条件

### 2.1 应用和 LSTM 序列

预测热应用为 Firefox、Thunderbird、VLC；预测冷应用为 GIMP、LibreOffice、Evince、ImageViewer、Solitaire。LSTM 五步历史固定为：

```text
Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC
```

两组使用完全相同的应用、切换动作和 seed `20260850`、`20260851`、`20260852`。五个预测冷应用均要求 `/dev/myfs` ABI v3 中存在有效 `FILE_DIRTY` 画像。

### 2.2 页面容量

| 页面池 | 容量 |
|---|---:|
| 五个冷应用干净冷页 | 120 MiB |
| 五个冷应用脏冷页 | 640 MiB |
| 三个热应用、随后精确复用的干净冷页 | 576 MiB |
| 定向回收缺口 | 512 MiB |

容量满足：

```text
cold_clean(120) < target(512) <= cold_clean+cold_dirty(760)
cold_clean(120)+hot_clean(576) >= target(512)
```

因此冷干净页本身不能完成回收。若冷脏页没有及时写回并变成可释放页，reclaim 必须继续触及热应用干净页或其他应用页面。

### 2.3 `may_writepage=0` 门禁

每轮实验临时设置：

```text
vm.laptop_mode=600
```

该设置使本轮 direct reclaim 初始进入 `may_writepage=0`。自动化在压力前再次读取 sysctl 并写入 `writeback-gate-evidence.json`；六轮均观察到 600。无论正常还是异常退出，runner 的 `finally` 都恢复原值；六轮结束后的实际值为 0。

增强组还要求 `writepage_promotions>0`，否则直接判为 `INVALID`。

### 2.4 脏页和压力时序

1. 启动 8 个真实 GUI 应用及其同 App ID 页面 fixture。
2. 建立训练集对齐的五步前台切换历史。
3. 最终 VLC 事件触发 LSTM 推理、workload 分类和 `/dev/myfs` 原子下沉。
4. 严格验证预测、8 个绑定以及五个冷应用的 `FILE_DIRTY` 画像。
5. 门禁通过后再次 `REDIRTY` 五个冷应用；fixture socket 不产生前台事件，因此不会改变 LSTM 历史。
6. 压力前验证三类文件至少 95% 驻留，且五个冷 cgroup 的 `file_dirty` 至少达到配置脏区域的 80%。
7. 将父实验 cgroup 的 `memory.max` 设置为当前用量加 512 MiB。
8. 在同一子树内按 64 MiB 连续申请 1024 MiB 匿名内存，形成约 512 MiB 定向回收缺口；`memory.swap.max=0`，压力后不额外等待。
9. 立刻采集每个 inode 的 `mincore()` 驻留变化。
10. 依次精确重读 Firefox、Thunderbird、VLC 各 192 MiB `clean.data`，再 warm 重读一次。

## 三、逐轮配对结果

### 3.1 回收来源与热页复用

| Seed | 策略 | 冷脏区域回收 | 热干净页回收 | 热页保留率 | 首次 file refault | major fault | 首次读取 | 精确复用耗时 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 20260850 | bin-only | 327.72 MiB | 2.47 MiB | 99.57% | 632 | 1 | 2.47 MiB | 27.94 ms |
| 20260850 | cold-aggressive | 366.44 MiB | 0 | 100% | 0 | 0 | 0 | 19.46 ms |
| 20260851 | bin-only | 253.38 MiB | 74.00 MiB | 87.15% | 41,282 | 2 | 161.26 MiB | 113.54 ms |
| 20260851 | cold-aggressive | 357.00 MiB | 0 | 100% | 0 | 0 | 0 | 18.13 ms |
| 20260852 | bin-only | 387.94 MiB | 0 | 100% | 0 | 0 | 0 | 18.33 ms |
| 20260852 | cold-aggressive | 355.44 MiB | 0 | 100% | 0 | 0 | 0 | 25.14 ms |

seed `20260851` 是最能体现机制的一组：cold-aggressive 多回收 **103.62 MiB** 冷脏来源，少回收 **74 MiB** 热干净页，使首次复用读取减少 **161.26 MiB**、耗时减少 **95.41 ms**。

seed `20260852` 中 bin-only 的原生 flusher 已及时完成写回，没有伤及热干净页；增强组因此没有新的 refault 收益，复用耗时反而增加 6.81 ms。这说明 cold-aggressive 的收益依赖实际缺口，支持按需门控而不是永久开启。

### 3.2 压力阶段代价

| Seed | 策略 | pgscan | pgsteal | 扫描回收效率 | 写入量 | PSI full | pswpout |
|---:|---|---:|---:|---:|---:|---:|---:|
| 20260850 | bin-only | 130,223 | 130,205 | 99.99% | 719.02 MiB | 51.02 ms | 0 |
| 20260850 | cold-aggressive | 130,350 | 130,260 | 99.93% | 674.98 MiB | 33.31 ms | 0 |
| 20260851 | bin-only | 129,711 | 129,710 | 100.00% | 739.83 MiB | 64.35 ms | 0 |
| 20260851 | cold-aggressive | 165,745 | 130,135 | 78.52% | 731.82 MiB | 30.96 ms | 0 |
| 20260852 | bin-only | 130,263 | 130,263 | 100.00% | 739.82 MiB | 52.01 ms | 0 |
| 20260852 | cold-aggressive | 130,232 | 129,736 | 99.62% | 738.89 MiB | 31.34 ms | 0 |

最差 seed `20260851` 中增强组多扫描 36,034 页，这是为更早、更深地处理预测冷 cgroup 支付的成本；但 `pgsteal` 基本不变，压力 PSI 和后续复用 I/O 均显著下降。当前样本下整体收益为正，但未来门控仍应设置 pgscan/PSI/I/O 预算。

## 四、内核动作证据

| 指标（三轮） | bin-only | cold-aggressive |
|---|---:|---:|
| `writepage_promotions` | 0 / 0 / 0 | 1,742 / 2,125 / 1,969 |
| `FILE_DIRTY` passes | 0 / 0 / 0 | 1,742 / 2,125 / 1,969 |
| workload profile hits | 0 / 0 / 0 | 1,742 / 2,125 / 1,971 |
| workload profile misses | 0 / 0 / 0 | 0 / 0 / 0 |
| pressure `pgscan_direct` | 130,223 / 129,711 / 130,263 | 130,350 / 165,745 / 130,232 |
| pressure `pgscan_kswapd` | 0 / 0 / 0 | 0 / 0 / 0 |

本场景最终使用父 cgroup `memory.max` 形成定向缺口，因此实际执行者是 direct reclaim，而不是 kswapd。Linux 原生代码仍不允许普通 direct reclaimer 同步 pageout 文件脏页；实际写回由 flusher 完成。cold-aggressive 在这里的作用是：在 `may_writepage=0` 时对预测冷、`FILE_DIRTY` cgroup 建立 promotion，并通过更早/更深的冷 cgroup 扫描使脏页进入 reclaim/writeback 流程，随后回收已写净页面。

因此，本报告证明的是 **cold-aggressive 整体路径**（预测冷筛选、priority boost、workload swappiness、writepage promotion 和原生 flusher 协同）有增量收益；它不能把全部收益单独归因于 `allow_writepage` 这个布尔值。

## 五、与第四场景合并后的策略结论

| 条件 | 建议 |
|---|---|
| 冷应用干净页已经足以覆盖预计回收量 | 只使用 LSTM + reclaim-bin；cold-aggressive 关闭 |
| 冷干净页不足、冷脏页足够、热应用页面面临风险、原生写回受限 | 有条件开启 cold-aggressive |
| 没有有效 `FILE_DIRTY` 画像或预测置信度不足 | 原生回退，不允许增强动作 |
| pgscan、PSI、写入或 swap 超过预算 | 立即退出 cold-aggressive，回到 bin-only |

建议后续门控输入为：

```text
required_reclaim
- predicted_cold_clean_reclaimable
= dirty_gap

仅当 dirty_gap > 0
且 predicted_cold_dirty >= dirty_gap
且 predicted_hot_reuse_cost > estimated_writeback_cost
时开启 cold-aggressive
```

## 六、有效性与限制

- 正式两组各 3 轮，6 轮全部有效；所有诊断/无效轮均排除。
- 六轮使用同一内核、相同 seed、相同页面容量和相同前台切换序列。
- 每轮压力前均验证 `laptop_mode=600`，结束后恢复为 0。
- 最低 `MemAvailable` 仍高于 8.3 GiB，没有 OOM、安全停止、swap-in 或 swap-out。
- 这是人为制造 `may_writepage=0` 和脏页缺口的机制验证，不代表普通桌面时刻都满足该条件。
- 只有三对样本，能够说明本机上的重复机制趋势，不能替代跨机器统计验证。
- 第五场景证明 bundled cold-aggressive 有条件获益；若要单独量化 `allow_writepage`，还需要增加“相同 priority/swappiness、只消融 allow_writepage”的内核开关。

## 七、原始数据与实现

- bin-only 正式三轮：[summary.json](/home/lzx/Desktop/PARP/test/outputs/cold_writeback_gate_hot_reuse/bin_lstm-20260830_143124-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin/summary.json)
- cold-aggressive 正式三轮：[summary.json](/home/lzx/Desktop/PARP/test/outputs/cold_writeback_gate_hot_reuse/bin_workload_lstm-20260830_143455-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin/summary.json)
- 第五场景配置：[parp-cold-writeback-gate-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-cold-writeback-gate-config-lzx.json)
- 实验 runner：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)
- 页面 fixture：[reclaim-substitution-fixture-lzx.py](/home/lzx/Desktop/PARP/test/test/reclaim-substitution-fixture-lzx.py)
- cold-aggressive 内核入口：[vmscan.c](/home/lzx/Desktop/PARP/lzx/kernel/src/linux-6.17.13-parp-lzx/mm/vmscan.c)
- workload/cold 判定：[watermark.c](/home/lzx/Desktop/PARP/lzx/kernel/src/linux-6.17.13-parp-lzx/mm/parp/core/watermark.c)
