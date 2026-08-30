# r8 冷脏页场景：bin-only 与 cold-aggressive 对比

日期：2026-08-28  
内核：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r8-cold-pressure #38`  
结论状态：三个 seed 配对完成，六轮全部 `VALID`。<!-- lzx-note -->

## 验证问题

验证在每个应用都具有相同页面局部状态的情况下，`LSTM + bin-reclaim` 与 `LSTM + bin-reclaim + cold-aggressive` 对预测冷应用冷脏文件页的回收率是否不同，同时检查是否误回收预测热应用。

这里区分两个问题：

1. 回收是否来自预测冷应用；
2. 在预测冷应用内部，回收的是否是指定的冷脏文件页。

两者不能用同一个 `memory.current` 降幅替代。第二个问题使用 `mincore()` 对每个独立文件 inode 的 resident 页进行压力前后精确计数。

## 场景设计

- 八个免登录 GUI 应用：Firefox、Thunderbird、VLC、GIMP、LibreOffice、Evince、ImageViewer、Solitaire。
- LSTM 历史固定为 `Thunderbird → Firefox → Thunderbird → Firefox → VLC`。
- Firefox、Thunderbird、VLC 为预测热应用；其余五个为预测冷应用。
- 每个应用与自己的受控 fixture 同处 `automation-<app>.scope`，使用同一个 `/dev/myfs` App ID 和 cgroup 绑定。
- 每个应用拥有 96 MiB 独立磁盘文件页和 32 MiB 私有匿名页。
- 文件页先逐页真实写入并 `fsync`，随后再次逐页修改但不 `flush/fsync`，形成 96 MiB mmap dirty 页；再调用 `MADV_COLD`，形成“resident、dirty、page-local cold”的受控页。
- 八个应用共有 768 MiB 受控冷脏页；五个预测冷应用共有 480 MiB。
- 回收目标为 384 MiB，预测冷应用容量是目标的 1.25 倍，理论上无需回收预测热应用。
- 压力前后分别执行 `mincore()`；该调用只查询 residency，不会把非 resident 页面重新 fault-in。

配置：[parp-cold-dirty-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-cold-dirty-config-lzx.json)  
runner：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)  
fixture：[memory-fixture-lzx.py](/home/lzx/Desktop/PARP/test/test/memory-fixture-lzx.py)

## 控制变量与有效性门

为了防止压力开始前由后台 flusher 随机清洗工作集，两组运行期间临时使用：

- `vm.dirty_background_bytes=2 GiB`；
- `vm.dirty_bytes=3 GiB`。

这两个值只阻止 768 MiB 受控数据在压力前被普通后台阈值提前清洗，不禁止压力回收写回。每轮 `finally` 都恢复原始 `dirty_background_ratio=10`、`dirty_ratio=20` 和两个 byte 值 0；六轮结果均保存了原值与恢复值，测试结束后的实时 sysctl 也已复核一致。

每轮必须同时满足：

- LSTM 历史、热应用排名和冷应用概率门通过；
- `/dev/myfs` ioctl 为 `APPLIED` 且至少有 8 个应用绑定；
- 每个应用压力前 resident 文件页不少于配置的 95%；
- 每个应用压力前 `file_dirty` 不少于配置的 80%；
- 压力前后是同一个 cgroup inode；
- bin 机制真实动作；增强组还要求 cold-aggressive `passes/scanned > 0`；
- 无 OOM、安全线中止或采集缺失。

## 正式结果

随机种子均为 `20260828/20260829/20260830`。

### 逐轮结果

| 策略 | seed | 冷应用脏页淘汰率 | 热应用脏页误淘汰率 | 被淘汰脏页的冷应用来源 | cgroup 写回 | PSI some | pgscan / pgsteal |
|---|---:|---:|---:|---:|---:|---:|---:|
| bin-only | 20260828 | 78.923% | 0.000% | 100.000% | 763.844 MiB | 48,580 us | 98,157 / 98,152 |
| bin-only | 20260829 | 75.322% | 3.640% | 97.182% | 771.730 MiB | 62,184 us | 98,161 / 98,152 |
| bin-only | 20260830 | 74.731% | 3.809% | 97.033% | 759.762 MiB | 31,652 us | 97,601 / 97,599 |
| bin + cold-aggressive | 20260828 | 30.090% | 0.000% | 100.000% | 1006.570 MiB | 635,077 us | 157,595 / 99,579 |
| bin + cold-aggressive | 20260829 | 29.556% | 0.000% | 100.000% | 1014.191 MiB | 392,844 us | 160,472 / 99,757 |
| bin + cold-aggressive | 20260830 | 40.082% | 0.000% | 100.000% | 697.562 MiB | 221,382 us | 148,752 / 99,441 |

### 三轮中位数

| 指标 | bin-only | bin + cold-aggressive | cold-aggressive 相对变化 |
|---|---:|---:|---:|
| 预测冷应用冷脏页淘汰率 | 75.322% | 30.090% | -60.05% |
| 预测冷应用冷脏页淘汰量 | 361.547 MiB | 144.430 MiB | -60.05% |
| 预测热应用冷脏页误淘汰率 | 3.640% | 0.000% | -100% |
| 预测热应用冷脏页误淘汰量 | 10.484 MiB | 0.000 MiB | -100% |
| 被淘汰受控脏页的冷应用来源 | 97.182% | 100.000% | +2.818 个百分点 |
| cgroup 写回量 | 763.844 MiB | 1006.570 MiB | +31.78% |
| `pgscan` | 98,157 | 157,595 | +60.55% |
| `pgsteal` | 98,152 | 99,579 | +1.45% |
| 扫描效率 `pgsteal/pgscan` | 99.995% | 63.187% | -36.808 个百分点 |
| cgroup PSI some | 48,580 us | 392,844 us | +708.65% |
| cgroup PSI full | 27,512 us | 381,134 us | +1285.34% |
| cold-aggressive 实际 passes | 0 | 1,049 | 机制已动作 |

正式输出目录：

- bin-only seed 20260828：`test/outputs/cold_dirty_reclaim/bin_lstm-20260828_204643-...`
- bin-only seed 20260829/30：`test/outputs/cold_dirty_reclaim/bin_lstm-20260828_204909-...`
- cold-aggressive seed 20260828：`test/outputs/cold_dirty_reclaim/bin_cold_lstm-20260828_204747-...`
- cold-aggressive seed 20260829/30：`test/outputs/cold_dirty_reclaim/bin_cold_lstm-20260828_205106-...`

早期 harness、I/O controller 和后台写回校准目录不进入上述汇总。<!-- lzx-note -->

## 结论

当前 cold-aggressive 在“应用间选择”上有效：三轮都没有淘汰预测热应用的受控冷脏页，被淘汰的受控脏页 100% 来自预测冷应用。bin-only 有两轮分别误淘汰约 3.64% 和 3.81% 的热应用受控脏页。

但是当前 cold-aggressive 不适合表述为“提高冷应用脏页回收率”。它将 bin 0 的 reclaim priority 提高 3 级，并把 swappiness 至少提高到 140。结果是内核在预测冷应用内部更积极地扫描匿名页和其他可回收页，并在达到相同总回收目标后提前停止；受控冷脏文件页只淘汰 30.09%，显著低于 bin-only 的 75.32%。额外扫描和写回还显著增加 PSI。

因此本轮证明的是：

> cold-aggressive 提高了应用间回收纯度并消除了热应用冷脏页误淘汰，但固定的高 swappiness 破坏了应用内页面类型选择，不能提高冷应用冷脏文件页淘汰率，且当前开销不合格。

## 后续机制改进建议

1. 将“应用回收强度”和“匿名/文件页偏置”拆开。保留 bin-0 priority boost，但不要无条件把 swappiness 拉到 140。
2. 在冷应用 `file_dirty/file` 比例高、冷文件页容量已能覆盖目标时，使用 file-first 模式：保持 `may_writepage=1`，但将 swappiness 保持原值或降低；匿名页占主导时再提高 swappiness。
3. 增加独立运行时开关与统计：`cold_pressure_boost`、`cold_anon_bias`、`cold_dirty_file_bias`，分别做消融，避免一个开关同时改变扫描量和页类型。
4. 内核侧补充 dirty-file scanned/writeback/reclaimed 计数；在此之前继续以独立 inode + `mincore` 作为精确外部证据。

在完成上述自适应页类型选择前，建议 cold-aggressive 继续默认关闭；当前可交付收益仍以 LSTM + bin-reclaim 的应用间排序为主。<!-- lzx-note -->
