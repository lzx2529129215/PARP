# Linux 6.17.13 Native 与 v4.2 r7 前台 LSTM + bin-reclaim 配对实验报告

日期：2026-08-28

实验主机：`lzx-virtual-machine`，32 GiB 内存

结论对象：仅验证“前台应用间 LSTM 预测 + reclaim-bin 排序”，不包含 effective-tier、Tier2 主动回收或 WSS 水位调节。

## 1. 结论

本轮 3 组内核、3 个场景、3 个相同种子共得到 27/27 个 VALID 样本。r7 的 LSTM + bin-reclaim 已经产生明确且可重复的收益：

1. `source_distribution` 中，APPLY 的冷应用回收来源中位数为 **99.03%**，Native 为 **50.13%**；热应用误回收由 Native 的 **386 MiB** 降到 **7.5 MiB**，下降 **98.06%**。
2. `future_reuse` 中，预测即将复用的 Firefox 在 APPLY 下保持 256 MiB 文件页全驻留；Native/OFF 则在压力阶段回收了大部分 Firefox 文件页。
3. Firefox 实际复用 128 MiB 页区间时，APPLY 的 file refault 为 **0**，Native 和 OFF 均为 **35,840 次/轮**；major fault 从 **1 次/轮降为 0**。
4. 实际复用延迟中位数由 Native 的 **95.834 ms** 降至 **56.850 ms**，下降 **40.68%**；相对 OFF 的 117.027 ms 下降 **51.42%**。
5. APPLY 的 memcg 子树 bin 动作稳定非零，OFF 全为 0；因此收益不是 LSTM 离线分数或日志变化，而是内核确实改变了回收对象。

本轮结果支持以下结论：在应用切换序列与训练集一致、预测准确的前提下，LSTM + bin-reclaim 能显著减少预计即将复用应用的 file refault、major fault 和复用延迟，同时优先回收低复用概率应用的冷页。

## 2. 对比组与开关

| 组别 | 内核 | PARP mode | effective-tier | Tier2 主动回收 | WSS 调节 | reclaim-bin | myfs |
|---|---|---:|---:|---:|---:|---:|---|
| Native | `6.17.13-native-6.17.13 #2` | 不存在 | 不存在 | 不存在 | 不存在 | 不存在 | 设备不存在，服务 fail-closed |
| r7 OFF | `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7 #34` | 0 | 0 | 0 | 无有效动作 | 0 | 预测仍下沉，但策略关闭 |
| r7 APPLY | 同上 | 2 | 0 | 0 | 0 | 1 | ABI v2，APPLIED |

APPLY 中 `memory.tier2_enabled=1` 只作为实验父 cgroup 的 bin-policy 所有权标记；全局 `vm.tier2_predict_enabled=0`，所以没有 Tier2 主动回收。OFF 捕获到 `vm.tier2_wss_predict_enabled=1`，但 Tier2 顶层开关为 0，WSS 路径不可执行。因此本报告以真正不含 PARP 代码的 Native 作为正式基线，r7 OFF 只用于同内核消融佐证。

## 3. 场景设计

- 应用集合共 8 个：
  - 高复用概率：Firefox、Thunderbird、VLC。
  - 低复用概率：GIMP、LibreOffice、Evince、Image Viewer、Solitaire。
- 每个应用绑定独立 GUI scope 与 fixture scope；每个 fixture 准备 256 MiB 文件工作集，其中 32 MiB 为前台热区。
- 先逐一使用 5 个冷应用一次，随后不再切回；再执行训练集对齐序列：
  - `Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC`
- v3 LSTM 的稳定输出：
  - Firefox：Top-1，概率约 79.29%。
  - Thunderbird：Top-2，概率约 13.22%。
  - 五个冷应用概率均小于 0.53%。
- 在相同父 cgroup 内增加 1 GiB 压力，并通过 cgroup threshold 强制约 768 MiB 文件页回收。
- 三个种子均为 `61713`、`61714`、`61715`，三组严格复用相同种子。
- `future_reuse` 在压力快照后切回 Firefox，并实际触碰偏移 32 MiB、长度 128 MiB 的文件页区间。

三个场景中，`cold_retire` 与 `source_distribution` 当前使用相同的压力动作骨架：前者用于验证“用过一次后不再使用的冷应用是否优先退出”，后者用于统计逐应用回收来源；它们不是两种完全独立的负载。`future_reuse` 在该骨架后额外执行真实复用触碰。

## 4. 有效性门槛

| 项目 | 结果 |
|---|---|
| 总样本 | 27/27 VALID |
| 每组样本 | 3 场景 × 3 种子 = 9 |
| LSTM 历史序列 | 27/27 完全匹配训练序列 |
| 热/冷概率门槛 | 27/27 通过 |
| APPLY myfs | APPLIED，ABI v2，无歧义绑定 |
| Native myfs | 设备不存在，服务 fail-closed；不影响 LSTM 观测与自动化 |
| 压力状态 | 27/27 到达 HOLDING |
| Host OOM | 0 |
| 安全中止 | 0 |

Native 和 r7 都出现同一类 VMware `vmwgfx/ttm` 图形驱动告警，发生在 plymouth/图形对象释放路径；告警栈不含 PARP，且不影响实验有效性。

## 5. 回收来源结果

以下均为 3 个相同种子的中位数。

### 5.1 source_distribution

| 指标 | Native | r7 OFF | r7 APPLY | APPLY 相对 Native |
|---|---:|---:|---:|---:|
| 冷应用回收占比 | 50.13% | 50.13% | **99.03%** | +48.90 个百分点 |
| 热应用回收占比 | 49.87% | 49.87% | **0.97%** | -48.90 个百分点 |
| 冷应用回收量 | 388.0 MiB | 388.0 MiB | **768.5 MiB** | +380.5 MiB |
| 热应用误回收量 | 386.0 MiB | 386.0 MiB | **7.5 MiB** | -98.06% |
| 总文件驻留页回收量 | 774.0 MiB | 772.0 MiB | 776.0 MiB | 基本等量 |
| direct scan | 199,088 | 198,744 | 198,665 | -0.21% |

总扫描量几乎相同是合理结果：三组都必须满足约 768 MiB 的相同回收目标。本方案的收益不是“少完成回收任务”，而是把相同回收量从即将复用的热应用转移到低复用概率应用。

### 5.2 source_distribution 逐应用回收量

| 应用 | 分类 | Native 中位数 | r7 OFF 中位数 | r7 APPLY 中位数 |
|---|---|---:|---:|---:|
| Firefox | 热 | 230 MiB | 230 MiB | **0 MiB** |
| Thunderbird | 热 | 78 MiB | 78 MiB | **7.5 MiB** |
| VLC | 热 | 78 MiB | 78 MiB | **0 MiB** |
| GIMP | 冷 | 78 MiB | 78 MiB | **242 MiB** |
| LibreOffice | 冷 | 78 MiB | 78 MiB | **242 MiB** |
| Evince | 冷 | 78 MiB | 78 MiB | **242 MiB** |
| Image Viewer | 冷 | 78 MiB | 78 MiB | **35 MiB** |
| Solitaire | 冷 | 76 MiB | 76 MiB | **7.5 MiB** |

Native/OFF 基本按原生 memcg/LRU 顺序平均分摊回收；APPLY 明确优先回收 GIMP、LibreOffice、Evince，并保护 Firefox、Thunderbird、VLC。不同冷应用的回收量不同，来自它们各自 LSTM rank 所映射的 8 个 bin，而不是简单的“所有冷应用等权”。

### 5.3 cold_retire

| 指标 | Native | r7 OFF | r7 APPLY |
|---|---:|---:|---:|
| 冷应用回收占比 | 50.26% | 36.65% | **99.03%** |
| 热应用误回收量 | 386.0 MiB | 490.38 MiB | **7.5 MiB** |
| 冷应用回收量 | 388.0 MiB | 283.64 MiB | **768.5 MiB** |

OFF 在该重复骨架上的轮间分布较大，而 APPLY 三轮为 99.032%–99.034%，说明预测排序结果比原生遍历起点更稳定。

## 6. future_reuse：压力后真实复用

### 6.1 压力阶段的回收来源

| 指标 | Native | r7 OFF | r7 APPLY |
|---|---:|---:|---:|
| 冷应用回收占比 | 50.26% | 52.87% | **99.03%** |
| 热应用误回收量 | 386.0 MiB | 364.0 MiB | **7.5 MiB** |
| Firefox fixture 被回收 | 大部分 | 大部分 | **0 MiB** |

### 6.2 Firefox 复用动作本身

这些指标来自 `snapshot-under-pressure.json -> snapshot-after-reuse.json`，即复用触碰前后的第二个时间窗口。此前 `run-result.json` 中的 `parent_deltas` 只覆盖加压阶段，不能用于判断复用后的 refault。

| 指标 | Native | r7 OFF | r7 APPLY | APPLY 相对 Native |
|---|---:|---:|---:|---:|
| 128 MiB 触碰延迟中位数 | 95.834 ms | 117.027 ms | **56.850 ms** | **-40.68%** |
| 延迟范围 | 67.445–133.082 ms | 71.816–178.630 ms | **56.342–70.194 ms** | 更稳定 |
| parent file refault | 35,840 | 35,840 | **0** | **-100%** |
| parent major fault | 1 | 1 | **0** | **-100%** |
| parent pgfault | 33,397 | 33,394 | **32,772** | -1.87% |
| 复用期间 parent direct scan | 52,321 | 52,224 | **16,640** | **-68.20%** |
| Firefox fixture direct scan | 6,656 | 6,656 | **0** | **-100%** |

`pgfault` 只下降约 1.9%，是因为 128 MiB 首次建立页表映射本身就需要约 32,768 次 minor fault，即使文件页仍驻留也无法消除。真正体现错误回收代价的是 file refault、major fault、复用期间 direct scan 和端到端触碰延迟，这四项都明显改善。

## 7. 内核动作证据

以 `source_distribution` 三轮中位数为例：

| 计数器 | r7 OFF | r7 APPLY |
|---|---:|---:|
| `subtree_passes` | 0 | 2,204 |
| `subtree_selected` | 0 | 5,927 |
| `subtree_skipped` | 0 | 31,962 |

APPLY 三轮分别产生 5,927、5,935、5,919 次子树选中动作；OFF 三轮均为 0。`run-result.json` 中 `apply_enabled=0` 是“结束状态减开始状态”的差值，不能解释为开关关闭；实际状态由 `policy-before.json` 中的 `reclaim_bin_enabled=1` 和上述动作计数共同证明。

r6 首轮曾出现 LSTM/myfs 正常但 `subtree_*` 全 0，原因是父实验 cgroup 没有 app 绑定，旧入口错误地要求父 cgroup 本身产生应用分数。r7 改为用父级 policy ownership 开启子树排序，再对各后代应用独立评分。修复记录：

`/home/lzx/Desktop/PARP/lzx/kernel/v4.2/patches/0016-parp-bin-order-enable-policy-subtree.patch`

## 8. 能说明什么，不能说明什么

可以说明：

- 在线 LSTM 预测已通过常驻服务触发，并通过 `/dev/myfs` 指导内核 bin-reclaim。
- 在与训练集一致且预测正确的应用切换场景中，bin-reclaim 能改变真实 memcg 回收来源。
- 该改变能转化为更低的 file refault、major fault、复用 direct reclaim 和用户可见复用延迟。
- r7 OFF 与 Native 在主来源分布上几乎一致，说明收益来自开启 bin 路径，而不是仅仅换成 PARP 编译内核。

不能说明：

- 不能代表训练集之外任意真实应用序列；本轮有意验证“模型预测正确时机制是否有效”。
- 不能代表 OOM 优化效果；本场景使用安全的 cgroup threshold，27 轮均未触发 OOM。
- 不能证明 effective-tier、Tier2 主动回收或 WSS 调节有效，因为它们在 APPLY 中均关闭。
- 三轮已显示方向稳定，但正式交付可进一步扩展到 10 轮以上并报告置信区间。

## 9. 原始数据

- r7 APPLY：`/home/lzx/Desktop/PARP/test/outputs/trained_sequence/bin_lstm-20260828_162736-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7`
- r7 OFF：`/home/lzx/Desktop/PARP/test/outputs/trained_sequence/parp_off-20260828_163641-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7`
- Native：`/home/lzx/Desktop/PARP/test/outputs/trained_sequence/native_kernel-20260828_165609-6.17.13-native-6.17.13`
- 场景配置：`/home/lzx/Desktop/PARP/test/test/parp-trained-sequence-config-lzx.json`
- 实验 runner：`/home/lzx/Desktop/PARP/test/test/parp-trained-sequence-experiment-lzx.py`

当前一次性 Native 启动已消费；GRUB 长期默认项仍为 `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7`，下次普通重启会自动返回 r7。
