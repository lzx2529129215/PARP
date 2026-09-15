# Native vs PARP r11：R6 串行复用与 R7 公平性/错误预测配对报告

日期：2026-09-02

结论：本轮 R6、R7 各 3 轮均为 `VALID`，PARP 六轮动作计划 SHA-256 均与对应 Native 轮次完全一致，且 PARP 的 `/dev/myfs` 下沉均为 `APPLIED`、ABI 3、8 个有效应用 cgroup 绑定。R6 明确验证了预测引导回收对串行 major-fault 关键路径的收益；R7 验证了预测引导的冷热应用回收来源隔离，并量化了故意错误预测后的恢复成本。<!-- lzx-note -->

## 1. 对比对象与实验约束

| 项目 | Native | PARP |
|---|---|---|
| 内核 | `6.17.13-native-6.17.13` | `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin` |
| 实验策略 | `native_kernel` | `bin_workload_lstm` |
| R6 seed | 20260921、20260922、20260923 | 严格重放相同 seed 和 Native action plan |
| R7 seed | 20260931、20260932、20260933 | 严格重放相同 seed 和 Native action plan |
| 回收请求 | 父 cgroup `memory.reclaim=512 MiB swappiness=max` | 完全相同 |
| 应用工作集 | 真实 GUI 应用操作；无测试 allocator | 完全相同 |
| runtime 预测 | 记录预测，但内核无 `/dev/myfs`，`FAIL_CLOSED` | `/dev/myfs` `APPLIED`，ABI 3 |

本轮 PARP 开关是：`reclaim_bin_enabled=1`、`reclaim_cold_enabled=1`、`reclaim_workload_enabled=1`；`effective_tier_mode=0`、全局 `tier2_enabled=0`、Tier2 工作集调节关闭。因此，本报告验证的是“LSTM 应用间预测 + bin 排序 + workload 感知冷回收”，不是 effective-tier 或 Tier2 主动回收的效果。虽然应用父 cgroup 的 `memory.tier2_enabled=1`，但全局 Tier2 关闭且 `reclaim_invocations=0`，本轮没有 Tier2 主动回收动作。<!-- lzx-note -->

数据根目录：

- R6 Native：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_114411-6.17.13-native-6.17.13`
- R6 PARP：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_workload_lstm-20260902_122307-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- R7 Native：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_121347-6.17.13-native-6.17.13`
- R7 PARP：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_workload_lstm-20260902_122828-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`

## 2. 配对与预测有效性

每个 PARP 轮次的 `action_plan_sha256` 与 Native 对应轮次一致：

| 场景 | seed | action plan SHA-256 前缀 | Native | PARP replay |
|---|---:|---|---|---|
| R6 | 20260921 | `b0211a4482d7` | VALID | VALID、匹配 |
| R6 | 20260922 | `a83cce0fce7b` | VALID | VALID、匹配 |
| R6 | 20260923 | `3966c34dcca2` | VALID | VALID、匹配 |
| R7 | 20260931 | `37f55a80576e` | VALID | VALID、匹配 |
| R7 | 20260932 | `756d87a2dde6` | VALID | VALID、匹配 |
| R7 | 20260933 | `a3924648b481` | VALID | VALID、匹配 |

正式压力前的固定历史为 `Thunderbird → Firefox → Thunderbird → Firefox → VLC`。PARP 每轮均观测到：

- Firefox：rank 1，概率约 0.78838；
- Thunderbird：rank 2，概率约 0.12686；
- GIMP：rank 13，概率约 `1.79e-6`；
- `/dev/myfs`：`ioctl_success=true`、`status=APPLIED`、ABI 3、`nr_bindings=8`、无歧义绑定；
- 8 个 workload profile 均有效。

这证明实验使用的不是写死在测试脚本中的冷热标签：runtime service 确实调用 LSTM，并把预测、应用 cgroup 绑定、工作集和 workload profile 一起下沉到了内核。<!-- lzx-note -->

## 3. R6：串行 major-fault 页面复用

### 3.1 场景动作

Firefox 页面物化 384 MiB 匿名工作集，并切分成 24 个 16 MiB 块。形成训练序列后，对八应用父 cgroup 回收 512 MiB；Firefox 返回前台后，24 次鼠标点击串行访问 24 个块。下一步必须等上一步的同步页面访问结束和标题确认后才开始，因此 major fault 不能被并发、固定 dwell 或其他应用的运行掩盖。

### 3.2 三轮原始结果

| 内核 | seed | 回收完成率 | 24 步总延迟 ms | 最慢一步 ms | major fault | refault | swap-in |
|---|---:|---:|---:|---:|---:|---:|---:|
| Native | 20260921 | 99.91% | 5979.32 | 760.84 | 21722 | 21755 | 21663 |
| Native | 20260922 | 99.85% | 8025.05 | 718.81 | 33056 | 33104 | 32976 |
| Native | 20260923 | 99.81% | 6141.60 | 446.24 | 32378 | 33663 | 33516 |
| PARP | 20260921 | 101.60% | 5039.91 | 235.17 | 357 | 411 | 411 |
| PARP | 20260922 | 101.50% | 5053.93 | 233.88 | 524 | 568 | 568 |
| PARP | 20260923 | 101.64% | 4920.07 | 241.57 | 658 | 724 | 724 |

### 3.3 中位数对比

| 指标 | Native | PARP | PARP 相对变化 |
|---|---:|---:|---:|
| 实际回收 | 511.21 MiB | 520.17 MiB | **+1.75%** |
| 预测热应用回收占比 | 64.64% | 2.78% | **-95.70%** |
| 预测冷应用回收占比 | 35.36% | 97.22% | **+174.94%** |
| 24 步串行复用总延迟 | 6141.60 ms | 5039.91 ms | **-17.94%** |
| 最慢单步延迟 | 718.81 ms | 235.17 ms | **-67.28%** |
| major fault | 32378 | 524 | **-98.38%** |
| refault | 33104 | 568 | **-98.28%** |
| swap-in | 32976 | 568 | **-98.28%** |

PARP 实际回收量略多于 Native，因此缺页下降不是“少回收”造成的。决定性变化是回收来源：Native 的回收中位数有 64.64% 来自预测热应用；PARP 将该比例压到 2.78%，把 97.22% 的回收转移到预测冷应用。Firefox 即将串行复用的匿名页因此大部分留在内存中，major fault/refault/swap-in 均下降约 98%，最终转化为 17.94% 的总关键路径缩短和 67.28% 的最慢单步延迟下降。总延迟降幅小于 fault 降幅，是因为点击分发、JavaScript 循环、标题确认等不随缺页数下降的固定成本仍然存在。<!-- lzx-note -->

## 4. R7：公平性与故意错误预测

### 4.1 场景动作

Firefox、Thunderbird、VLC、GIMP 先完成真实 GUI 工作集预热；GIMP 逐张操作 6 张 4096×4096 图像并执行 Invert。随后形成固定训练历史，使 GIMP 被预测为极冷，再回收 512 MiB。回收后按相同动作重用四个应用，其中故意重新操作低概率 GIMP，构造“模型认为不会返回，但用户实际返回”的错误预测。

### 4.2 回收来源和公平性中位数

| 指标 | Native | PARP | PARP 相对变化 |
|---|---:|---:|---:|
| 实际回收 | 510.89 MiB | 522.46 MiB | **+2.26%** |
| 预测热应用回收占比 | 55.33% | 2.02% | **-96.36%** |
| 预测冷应用回收占比 | 44.67% | 97.98% | **+119.36%** |
| Jain normalized responsiveness | 0.9196 | 0.9590 | **+4.28%** |
| 全应用最大 normalized slowdown | 1.4719× | 1.1038× | **-25.01%** |

PARP 把回收集中到预测冷应用，而不是平均伤害所有应用。三个预测热应用的被回收量中位数变化为：Firefox `164.04 → 5.57 MiB`，Thunderbird `107.43 → 4.69 MiB`，VLC `11.66 → 0.43 MiB`。对应的 major fault 分别下降 93.64%、83.99% 和 93.62%。

按应用观察 normalized slowdown，Firefox `1.4719× → 0.6708×`、Thunderbird `1.2315× → 1.1038×`；VLC `0.9733× → 1.0642×`，即 VLC 有约 9.34% 的轻微退化。Jain 指数提升和最大退化下降说明总体公平性改善，但 VLC 的退化仍需在更大样本中观察，不能用整体指标掩盖。<!-- lzx-note -->

### 4.3 GIMP 错误预测代价

| 指标 | Native | PARP | PARP 相对变化 |
|---|---:|---:|---:|
| GIMP 实际回收 | 62.59 MiB | 405.29 MiB | **+547.49%** |
| GIMP major fault | 3041 | 0 | **-100.00%** |
| GIMP refault | 5005 | 2885 | **-42.36%** |
| GIMP swap-in | 4989 | 0 | **-100.00%** |
| GIMP major + refault | 8128 | 2885 | **-64.51%** |
| GIMP additional page-in recovery | 56.72 ms | 36.51 ms | **-35.64%** |
| GIMP 混合 GUI normalized slowdown | 0.6624× | 0.8996× | +35.81% |

这里“PARP 回收 GIMP 更多，但 major fault 和 swap-in 更少”并不矛盾：

- Native 从 GIMP 回收的中位数组成约为 61.64 MiB 匿名页、0.49 MiB 文件页，形成约 4989 次 swap-in；
- PARP 从 GIMP 回收的中位数组成约为 0 MiB 匿名页、394.96 MiB 干净文件页，形成 0 次 swap-in；
- 干净文件页通过普通 `read()` 路径重新进入 page cache 时可以计入 workingset refault，但不一定经过用户页错误处理，因此不要求同步出现 `pgmajfault`；
- GIMP 后续动作只重用被回收集合中的一部分，所以约 405 MiB 被回收不等于全部页面都会立即 refault。

因此，R7 的结论不是“错误预测没有代价”，而是：在这个错误预测下，bin + workload 感知把代价从匿名页换出/换入转成了可丢弃干净文件页的按需重读。故意返回 GIMP 后仍有 2885 次 refault 和 36.51 ms 额外 page-in 等待，但它们均低于 Native；与此同时三个预测热应用得到明显保护。GIMP 的混合 GUI slowdown 两侧都小于 1，说明滤镜缓存、undo 栈和命令执行波动仍会让回收后操作偶尔快于预热操作，因此该字段不能单独代表页面恢复成本，需与独立 page-in 等待和 fault/refault 一起解释。<!-- lzx-note -->

## 5. 内核动作证据

PARP 每轮均出现真实的 bin 排序和 cold/workload 回收动作：

- R6 每轮 `context_hits` 为 1998、2269、2668，`subtree_selected` 为 320、364、431；cold 路径分别回收 128949、127339、127103 页。
- R7 每轮 `context_hits` 为 2458、2472、2529，`subtree_selected` 为 391、399、405；cold 路径分别回收 128396、128515、128428 页。
- R6/R7 均有 workload profile 命中和按类别扫描；R7 三轮 `file_clean_passes` 为 53、55、55。
- `rebin_moves=0` 不代表策略未生效。当前实现是在扫描时对 cgroup 子树评分、选择或跳过，并不把 cgroup 永久迁移到另一个物理 bin；本轮生效证据应看 `context_hits`、`rank_scores`、`subtree_selected/skipped` 和最终回收来源分布。

## 6. 可得结论与边界

本轮可以得出：

1. **R6 机制和端到端收益验证通过。** 在同 seed、同动作计划、回收量不低于 Native 的条件下，PARP 显著减少即将复用应用的匿名页换出，串行 major-fault 关键路径得到稳定收益。
2. **R7 冷热来源隔离和公平性验证通过。** PARP 将约 98% 的回收集中到预测冷应用，使三类预测热应用少回收、少 fault，并提升 Jain 指数、降低最坏应用退化。
3. **错误预测代价已被实际触发并量化。** GIMP 确实被预测为极冷、被多回收并随后重用；workload 感知避免了昂贵匿名页换入，使错误成本主要表现为文件 refault。

本轮不能直接得出：

- 不能据此宣称所有真实 PC 序列都有相同收益；序列有意贴合训练集，且只有 3 个 seed。
- 不能宣称 PSI 已改善。两侧应用 PSI `some` 均为 0；32 GiB 环境中的定向 `memory.reclaim` 没有形成可测的进程级持续 stall。R6/R7 的有效指标是串行恢复延迟、fault/refault/swap-in、来源分布和公平性，而不是 PSI。
- 不能把本结果归因于 effective-tier 或 Tier2；二者在本轮关闭。后续若验证完整 APPLY，需要在同一 R6/R7 动作计划上分别打开它们做消融。
- GUI 应用内部缓存和工作集大小仍会跨启动波动。固定 action plan、固定回收请求和强有效性门禁降低了影响，但正式验收仍建议增加轮数，并加入非训练序列与全局压力阶段。<!-- lzx-note -->
