# R6/R7 串行热页复用、公平性与错误预测代价实验设计

日期：2026-09-02

状态：自动化场景、指标采集、严格动作重放和有效性门禁已经实现；Native 与 PARP r11 已完成 R6/R7 各 3 轮严格配对。正式结果见 `native-vs-parp-r11-r6-r7-serial-fairness-20260902-lzx.md`。<!-- lzx-note -->

## 1. 延迟指标修正

旧 `interaction_latency.successful_total_ms` 是 `REAL_REUSE_*` 自动化命令耗时总和，其中包含按键重复间隔、文本逐字符输入和固定 dwell。该指标保留用于兼容，但不再单独作为用户体验结论。

新增指标：

- `successful_net_total_ms`：从动作耗时中扣除测试脚本主动加入的按键、文本和 wait 节奏。
- `responsive_spans[].gross_ms`：从窗口切换请求开始，到应用画面完成变化并连续三帧近似稳定。
- `responsive_spans[].net_responsive_ms`：在上述跨度上进一步扣除已知脚本节奏。
- 每次画面稳定检测使用 32×32 灰度缩略采样，允许极小的光标/时钟变化；必须先相对操作前画面达到变化阈值，再连续三次稳定。旧画面未变化不能通过。<!-- lzx-note -->

## 2. R6：串行 major-fault 热页复用

场景名：`r6_app_serial_major_reuse`

### 2.1 自动化动作

1. 启动并操作八个真实 GUI 应用，建立各自 application scope 和真实应用工作集。
2. Firefox 打开离线 `serial-reuse.html`，页面自身在浏览器进程内申请并实际写入 384 MiB 匿名页。
3. 工作集分为 24 个 16 MiB 块。初始化按确定性跨页步长写入，避免页面未物化。
4. 重放 `Thunderbird → Firefox → Thunderbird → Firefox → VLC` 训练序列。Firefox 的窗口切换不会再次触碰上述匿名数组，因此页面在原生页面年龄意义上可以变冷，但应用间预测仍把 Firefox 判为高概率。
5. 向八应用父 cgroup 请求回收 512 MiB，并在同一个 cgroup v2 `memory.reclaim` 请求上设置双方一致的 `swappiness=max`，保证 R6 专门形成匿名页换出，而不是再次只回收充足的文件页。
6. Firefox 返回前台后，自动化按 24 步点击页面中央的大型操作按钮。每一步由浏览器前台主线程以另一组确定性跨页步长，同步读写一个 16 MiB 块；该块的全部页面访问完成后立即更新窗口标题。鼠标点击避免浏览器外壳吞掉快捷键，下一次点击仍必须等待上一步标题确认。这里的延迟终点明确是“主线程串行页面访问完成”，不混入可能受合成器节流影响的下一帧调度时间。
7. 自动化从按键请求开始等待对应 `PARP R6 STEP xx/24` 标题，逐步记录关键路径延迟。下一步只有在上一步完成后才能开始，换入不能通过多应用并行或固定输入等待被掩盖。<!-- lzx-note -->

### 2.2 有效性与指标

- 必须完成 24/24 个标题确认步骤。
- Native 基线 `FIREFOX.pgmajfault` 必须达到配置下限 256；否则说明基线没有形成足够的串行换入，本轮无效。
- PARP 不要求 major fault 下限，因为把 Firefox 页面保留在内存中正是预期收益。
- 输出 `serial-major-reuse-result.json`：逐步延迟、总延迟、最大单步延迟、major fault、refault 和 swap-in。
- 同时保留回收来源、Firefox 被回收量、I/O 和 PSI，用于排除“PARP 只是少完成回收”的伪收益。实际父 cgroup 回收量仍必须达到相同比例。<!-- lzx-note -->
- `swappiness=max` 同时用于 Native 和 PARP 严格重放；它只固定 R6 的匿名页类型，不指定从哪个应用回收，应用来源选择仍由 Native MGLRU 或 PARP 预测策略决定。<!-- lzx-note -->

## 3. R7：公平性和错误预测代价

场景名：`r7_app_fairness_misprediction`

参与应用：Firefox、Thunderbird、VLC、GIMP。故意错误复用应用：GIMP。

### 3.1 自动化动作

1. 先对 GIMP 做一轮不计分的 Invert 预热，排除首次命令搜索、滤镜初始化和 undo 缓冲创建成本。随后四个参与应用分别执行一次真实 GUI 操作，记录无压力热态的“切换到 application cgroup 的 major-fault、swap-in、refault 计数连续稳定”时间，作为每个应用自身的基线。该终点不依赖 Wayland 下无法可靠读取的 XWayland 窗口像素。GIMP 会逐张切换六张 4096×4096 图像并通过命令搜索执行 Invert，确保真正访问解码后的图像工作集。
2. 随后重放固定训练序列，覆盖前述公平性探测产生的前台历史。
3. 门禁要求 Firefox/Thunderbird 为预期高排名，并要求 GIMP 的预测概率不高于 0.01。
4. 向应用父 cgroup 请求回收 512 MiB，并在 Native/PARP 两侧一致使用 `swappiness=max`，让错误预测代价能够覆盖匿名/交换页而不被充足的干净文件页掩盖。低概率 GIMP 还必须实际被回收至少 32 MiB；该门槛只证明错误复用有真实回收来源，主要强度门槛仍是 Native 的 fault 数。
5. 回收后按相同顺序、相同动作再次操作四个应用。GIMP 虽被预测为冷，仍被故意切回前台，从而把“低概率但用户意外返回”变成可重复的错误预测事件。
6. 每个应用都以自身的热态延迟归一化，避免把 Firefox、VLC 和 GIMP 不同操作的绝对耗时直接比较。<!-- lzx-note -->

### 3.2 指标解释

- `normalized_slowdown = post_reclaim_net_responsive_ms / warm_net_responsive_ms`。
- `normalized_responsiveness = 1 / normalized_slowdown`。
- 对四个应用的 normalized responsiveness 计算 Jain 指数；越接近 1，应用间退化越均衡。
- `unexpected_app_cost` 单独记录 GIMP 的被回收量、slowdown、pgfault、major fault、refault、swap-in 和 PSI。
- 同时单独汇总每个 `wait_cgroup_pagein_stable` 超出固定 200 ms 防抢跑窗口的净耗时，并输出 `additional_pagein_recovery_ms = max(0, post_pagein_wait - warm_pagein_wait)`；该字段用于隔离页面恢复成本，避免 GIMP 滤镜缓存/undo 成本把 fault 延迟掩盖在混合 slowdown 中。
- 正式 Native 门禁要求 GIMP 至少被回收 32 MiB，且重新操作产生的 `major fault + refault` 至少为 128；PARP 不强制 fault 下限，以免把“成功保留页面”反判为无效，但仍要求 GIMP 实际被回收至少 32 MiB。
- 公平性不是要求冷热应用回收量相同。预测策略可以有意保护高概率应用，但必须同时公开错误预测时低概率应用付出的代价，避免只报告受益应用。
- 最终需要同时比较：高概率应用收益、GIMP 错误预测损失、全体 Jain 指数以及父 cgroup 实际回收量。<!-- lzx-note -->

## 4. 严格配对协议

Native 和 PARP 必须使用相同配置、seed、资产 SHA-256 和 `action-plan.json`。PARP 使用 `--replay-from` 指向 Native 输出目录；动作哈希不一致直接 `BLOCKED`。

建议每个场景先运行 3 轮校准。R6 Native 若 major-fault 门禁有失败轮次，应调整场景专用回收目标或匿名工作集大小后同时废弃两侧旧轮次，不能只调整 PARP。R7 不应以 Jain 指数门槛判实验成功或失败，它是需要如实报告的策略权衡指标。<!-- lzx-note -->

## 5. 当前验证

- R1–R7 场景生成与动作计划测试：11/11 通过。
- automation 单元测试：19/19 通过。
- Python 编译检查：通过。
- `git diff --check`：通过。
- 离线资产 manifest 已升级到 schema 5，并包含确定性 `serial-reuse.html`。

### 5.1 正式 Native 基线（2026-09-02）

内核：`6.17.13-native-6.17.13`。以下根目录只包含最终设计下的三轮 VALID 数据；此前 INVALID/校准目录不进入统计。

- R6 正式根目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_114411-6.17.13-native-6.17.13`
- R7 正式根目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_121347-6.17.13-native-6.17.13`

R6 三轮均完成 24/24 步，512 MiB 回收实际完成率分别为 99.91%、99.85%、99.81%。三轮中位数：

| 指标 | Native 中位数 |
|---|---:|
| 串行页面复用总延迟 | 6141.60 ms |
| 最慢单步延迟 | 718.81 ms |
| Firefox major fault | 32378 |
| Firefox refault | 33104 |
| Firefox swap-in | 32976 |

R7 最终版三轮均满足 GIMP 低概率、实际回收来源和 Native fault 门槛。三轮中位数：

| 指标 | Native 中位数 |
|---|---:|
| 父 cgroup 回收完成率 | 99.78% |
| Jain normalized responsiveness | 0.9196 |
| 全应用最大 normalized slowdown | 1.4719× |
| GIMP 实际回收 | 62.59 MiB |
| GIMP major fault | 3041 |
| GIMP refault | 5005 |
| GIMP swap-in | 4989 |
| GIMP major+refault | 8128 |
| GIMP additional page-in recovery | 56.72 ms |
| GIMP 混合 GUI normalized slowdown | 0.6624× |

GIMP 的混合 GUI slowdown 小于 1，说明 GIMP 滤镜缓存、undo 栈和命令执行波动仍大于单轮页面恢复延迟，不能单独用该值判定错误预测没有代价。fault/swap-in 与独立的 56.72 ms 页面恢复额外耗时共同证明错误复用确实发生；PARP 配对结果需同时比较这些字段。<!-- lzx-note -->

### 5.2 正式 PARP r11 配对结果（2026-09-02）

PARP 已使用 `--replay-from` 严格重放上述 Native R6/R7 根目录；6 个 PARP 轮次全部 `VALID`，每轮动作哈希均与对应 Native 完全一致，`/dev/myfs` 均为 `APPLIED`、ABI 3、8 个有效绑定。

核心中位数结果：R6 major fault `32378 → 524`（-98.38%）、refault `33104 → 568`（-98.28%）、swap-in `32976 → 568`（-98.28%）、24 步串行复用总延迟 `6141.60 → 5039.91 ms`（-17.94%）、最慢单步 `718.81 → 235.17 ms`（-67.28%）。R7 的预测热应用回收占比 `55.33% → 2.02%`，Jain 指数 `0.9196 → 0.9590`；故意错误复用的 GIMP 被回收量 `62.59 → 405.29 MiB`，但 workload 感知将来源从匿名页切换为干净文件页，使 swap-in `4989 → 0`、major+refault `8128 → 2885`、额外 page-in 恢复 `56.72 → 36.51 ms`。

完整逐轮数据、机制解释和结论边界见 `native-vs-parp-r11-r6-r7-serial-fairness-20260902-lzx.md`。<!-- lzx-note -->
