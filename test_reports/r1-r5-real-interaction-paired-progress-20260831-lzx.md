# R1-R5 应用原生交互配对实验进度（2026-08-31）

## 实验目标

在完全相同的随机种子与 `action-plan.json` 下，对比 Linux 6.17.13 Native 与 PARP R11：

- Native：`6.17.13-native-6.17.13`
- PARP：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 固定随机种子：`20260831`
- 场景：R1-R5 应用原生交互场景
- R1-R4 主对比：`native_kernel` vs `bin_lstm`
- R5 机制对比：`bin_lstm` vs `bin_workload_lstm`

## 公平性约束

1. Native 首先生成每轮 `action-plan.json`。
2. PARP 使用 `--replay-from <Native session>`，逐项校验动作计划哈希后重放。
3. 应用、离线资产、动作顺序、随机种子、回收目标和复用动作保持一致。
4. 不使用匿名页压力 fixture；工作集由 Firefox、Thunderbird、VLC、GIMP、LibreOffice、Evince、Image Viewer、Solitaire 的真实应用操作建立。
5. 压力只通过父 cgroup `memory.reclaim` 施加到上述应用已建立的工作集。

## 当前进度

- [x] 当前 PARP 内核确认：R11 workload-cold-bin。
- [x] `/dev/myfs` 确认存在。
- [x] 常驻预测服务恢复为 `active`。
- [x] 内存与磁盘空间检查通过（15 GiB RAM，根分区可用 91 GiB）。
- [x] 启动 Native。
- [x] Native 校准轮（旧诊断数据均已排除，最终 R2/R4/R5 定向校准有效）。
- [x] Native 正式轮次：R1-R5 × 3，15/15 `VALID`。
- [x] 启动 PARP R11。
- [x] PARP `bin_lstm` 精确重放：R1-R5 × 3，15/15 `VALID`，15 个动作计划 SHA-256 与 Native 逐轮一致。
- [x] R5 `bin_workload_lstm` 应用原生校准：稳定页面类型合同为 GIMP=`FILE_CLEAN`、LibreOffice=`ANON_HEAVY`，单轮 `VALID`；130 次 workload-aware cold pass，97.49% 的应用回收量来自预测冷应用，热应用复用期 major fault/refault/swap-in 均为 0。该轮只作校准，不纳入配对性能结论。
- [x] 以新版 R5 动作计划重新生成 Native × 3 基线：3/3 `VALID`。
- [x] PARP `bin_workload_lstm` 按新版 Native R5 精确重放 × 3：3/3 `VALID`，SHA-256 逐轮一致。
- [x] 生成最终指标与结论报告：`r1-r5-app-native-native-vs-parp-r11-final-20260902-lzx.md`。

## 结果目录

实验数据将写入：

`/home/lzx/Desktop/PARP/test/outputs/real_interaction`

## Native 校准诊断记录

以下轮次只用于校准，明确不纳入最终 Native/PARP 结论：

1. `native_kernel-20260831_175339-6.17.13-native-6.17.13`
   - runtime service 的生产 cgroup router 将 GUI 从 `automation-*.scope` 二次迁移到 `parp-route-*.scope`；
   - 导致 per-App 指标 scope 缺失、清理无法杀掉迁移后的 GUI；
   - Epiphany 首次空白页未命中 Firefox 窗口；
   - R1-R3 缺少可选 `io.stat` 时采集器异常退出。
2. `native_kernel-20260831_181001-6.17.13-native-6.17.13`
   - 上述三项均已修正且测试通过；
   - 但运行前桌面已自动锁屏，`_NET_ACTIVE_WINDOW` 固定为 `0x0`；
   - 因而没有真实 `APP_SWITCH`，LSTM 历史门禁按预期拒绝轮次。

已启动 `parp-paired-inhibit.service` 抑制实验期间再次空闲锁屏。用户解锁后重新进行 Native 校准。

3. `native_kernel-20260901_193421-6.17.13-native-6.17.13`
   - R1 有效；R2-R4 的 Epiphany 小型瞬态窗口被 X11 自动化误判为 Firefox，但 GNOME 前台监听观察到 Desktop，破坏了五步训练历史；
   - R5 的真实 GUI 总工作集约 2.02 GiB，但应用保存后仍驻留的 `file_dirty` 只有约 152 KiB；
   - 已增加 Firefox 大内容窗口几何门禁与小型瞬态窗口关闭/重试；
   - R4/R5 应用原生门禁改为分别记录 `file_dirty` 和匿名脏页，并要求二者组成的高代价冷页至少 384 MiB。匿名页没有干净文件后备，回收必须 swap-out；不能把它与普通干净文件缓存混为一谈。

## 有效 Native 基线

- 目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260901_195238-6.17.13-native-6.17.13`
- 轮次：R1-R5 各 3 轮，共 15 轮。
- 状态：15/15 `VALID`。
- 轮次种子：`20260831`、`20260832`、`20260833`。
- 每个场景/轮次均保存独立 `action-plan.json` 与 SHA-256；PARP 端必须通过 `--replay-from` 精确匹配，哈希不一致即拒绝。
- 下一启动项已设置为 `6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`，等待重启后执行 APPLY 重放。

## 有效 PARP bin+LSTM 重放

- 目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_lstm-20260901_205141-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 轮次：R1-R5 各 3 轮，共 15 轮，15/15 `VALID`。
- 每轮均通过 `/dev/myfs` ABI v3 下沉 8 个无歧义应用绑定；Firefox 为 Top-1（约 0.777），Thunderbird 为 Top-2（约 0.126）。
- runtime service 全程 `NRestarts=0`；为避免压力期间全量 eBPF 文件事件淹没严格事件通道，本阶段只关闭不被 bin+LSTM 消费的文件事件流，workload profile 仍直接来自各 cgroup 的 `memory.stat`。

## R5 应用原生 workload 校准结论

前三次失败校准均明确排除，不纳入结果：

1. GIMP 4096×4096 图片仍停在 `Open Image` 对话框，拖拽没有落到画布。
2. 把最终 LSTM gate 错误地同时要求为 `FILE_DIRTY`；策略如果先处理脏页，最终 profile 会变为 `FILE_CLEAN`，导致把真实动作误判成失败。
3. 图片完全载入并通过 GIMP 真实反相操作编辑后，GEGL tile-swap 会自行写净；偶发的数百 MiB `file_dirty` 是异步导入瞬间，不是稳定用户态。

因此新版 R5 将应用原生稳定合同定义为 GIMP=`FILE_CLEAN`、LibreOffice=`ANON_HEAVY`，验证按 workload 调整匿名页/文件页扫描；不再把它声明成脏文件页 cold-aggressive/writepage 验证。原始 `FILE_DIRTY + may_writepage=0` 机制仍需保留受控 fixture 场景，真实 GUI 版不能替代它。

- 有效校准目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_workload_lstm-20260901_213026-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 后续已完成：新版 Native 基线与 R11 严格回放各三轮。

## 新版 R5 Native 正式基线

- 目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/native_kernel-20260902_100838-6.17.13-native-6.17.13`
- 状态：3/3 `VALID`；种子 `20260911`、`20260912`、`20260913`。
- 动作计划 SHA-256：`97c17421ef341a1c1cba18a3ca5da03115c324f0b1cc45fceb6524a87b719016`、`7097405ae4e4949b02b6913ac6c0406ada4999d62c04facc66175b0b9cf00d92`、`e43f959097c3759a46db3622eee3265f92a7d65d0ac1d139cf1c53d883c14a6b`。
- 三轮中位数：冷应用回收占比 71.66%，热应用回收 73.90 MiB；热应用复用期 major fault 1398、refault 1892、swap-in 1818、交互动作总延迟 4761 ms。
- 后续已完成：R11 已以该目录为 `--replay-from` 完成三轮。

## 新版 R5 PARP 正式重放

- 目录：`/home/lzx/Desktop/PARP/test/outputs/real_interaction/bin_workload_lstm-20260902_102537-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 状态：3/3 `VALID`，三个动作计划 SHA-256 与新版 Native 逐轮一致。
- 三轮中位数：冷应用回收占比 96.49%，热应用回收 9.21 MiB；热应用复用期 major fault 1、refault 1、swap-in 1、交互动作总延迟 4940 ms。
- 最终报告：`/home/lzx/Desktop/PARP/test_reports/r1-r5-app-native-native-vs-parp-r11-final-20260902-lzx.md`。
