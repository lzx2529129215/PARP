# r11 LSTM 驱动内存回收：验证方法与结果说明

**日期：** 2026-08-30  
**结论先行：** 当前已经证明“LSTM 应用间预测能够通过 `/dev/myfs` 影响 r11 的 cgroup reclaim 选择，并把回收从预测热应用迁移到预测冷应用”。同时也已经证明 workload-aware 的四种匿名/文件页策略会被内核实际调用。**但尚未证明 workload-aware 能降低真实用户返回应用后的卡顿、PageFault 或 PSI；现有数据反而显示它相对 bin-only 增加了 swap 和 direct reclaim。**

---

## 1. 要验证的不是一个问题，而是三层问题

这套实验把“优化是否有效”拆成三个不可混淆的问题：

| 层次 | 要回答的问题 | 当前状态 |
|---|---|---|
| 预测链路 | LSTM 是否真的在正确的 GUI 序列下给出“热/冷应用”判断，并下沉至内核？ | 通过 |
| 回收机制 | 内核是否真的少回收热应用、多回收预测冷应用，并执行指定的页面类型策略？ | 通过 |
| 用户收益 | 用户重新使用被保护的热应用时，fault/refault、swap-in、交互延迟和 PSI 是否下降？ | 尚未完成 |

不能因为前两层通过，就直接说“真实 PC PageFault 已下降”。前两层证明机制存在且可控；第三层需要一个压力结束后的确定性工作集重访场景。

## 2. 被验证的完整链路

```text
真实 GUI 前台切换
        │
        ▼
runtime_monitor 常驻服务
  ├─ 监听启动 / 切换 / 关闭 / 最小化
  ├─ 以最近 5 次应用序列运行 LSTM
  └─ 读取每个应用 cgroup 的 anon / file / file_dirty 画像
        │
        ▼
/dev/myfs ABI v3 原子下沉
  ├─ 应用 reentry 概率与排名
  └─ 每个 cgroup 的 workload profile
        │
        ▼
r11 内核（仅 Apply 开关打开时）
  ├─ 预测冷且 profile 有效、概率 ≤ 1%：进入 reclaim bin 0
  ├─ 预测热应用：保留在较高 reclaim bin
  └─ workload-aware：按 profile 调整 anon/file 倾向
        │
        ▼
cgroup reclaim、mincore 驻留、memory.stat、PSI、swap/refault 取证
```

Native 组保留相同的 runtime service、桌面监听、内存画像和 LSTM 推理，使服务的 CPU/内存开销一致；只是 Native 内核不存在 `/dev/myfs`，因此 service 在被动模式运行，不向内核下沉预测。

## 3. 内核和消融组

| 组别 | 内核 | service | 内核策略 |
|---|---|---|---|
| Linux Native | `6.17.13-native-6.17.13` | 监听、画像、LSTM 均运行；不写 `/dev/myfs` | 原生 MGLRU |
| Bin + LSTM | `...r11-workload-cold-bin` | `/dev/myfs` ABI-v3 APPLY | 仅 LSTM 应用概率驱动 reclaim-bin；关闭 cold-aggressive 和 workload-aware |
| Bin + LSTM + workload | 同上 | `/dev/myfs` ABI-v3 APPLY | reclaim-bin + 预测冷一轮压力 + per-cgroup workload profile |

因此，Bin+LSTM 相对 Native 的新增变量是“预测驱动的 cgroup 回收选择”；workload-aware 相对 Bin+LSTM 的唯一新增变量是“根据每个 cgroup 工作集画像改变匿名/文件页回收倾向”。

### r11 的关键修正

此前仅用 LSTM **排名**映射 reclaim bin：某应用即使原始重入概率很小，也可能因排名第七而被放在较晚的 bin，来不及被扫描。r11 改为：

- 仅在 `bin_workload_lstm` 显式打开时；
- `/dev/myfs` V3 profile 必须有效且未过期；
- 非前台应用的原始概率必须 `≤ 327 / 32767`，约 1%；
- 满足这些条件时，直接分入 bin 0。

这不会改变 Native 或普通 `bin_lstm` 的稳定排名分桶，目的是验证“明确低概率的应用应在第一轮优先回收”。

## 4. 受控的真实桌面场景

实验是真实 GUI 应用 + 受控页面工作集，不是只启动一个匿名内存分配器。

### 4.1 应用与 LSTM 序列

热应用：Firefox、Thunderbird、VLC。  
预测冷应用：GIMP、ImageViewer、Evince、LibreOffice、Solitaire。

固定训练序列为：

```text
Thunderbird → Firefox → Thunderbird → Firefox → VLC
```

三轮中 prediction gate 都通过。典型预测为：

| 应用 | LSTM 概率 | 排名 | 场景角色 |
|---|---:|---:|---|
| Firefox | ≈85.5% | 1 | 预测热 |
| Thunderbird | ≈9.3% | 2 | 预测热 |
| Solitaire | ≈0.198% | 6 | 预测冷 |
| ImageViewer | ≈0.060% | 7 | 预测冷、MIXED |
| Evince | ≈0.010% | 9 | 预测冷、FILE_DIRTY |
| GIMP | ≈0.00047% | 12 | 预测冷、ANON_HEAVY |
| LibreOffice | ≈0.00022% | 13 | 预测冷、FILE_CLEAN |

这意味着“热/冷”不是由测试直接写死给内核，而是由该 LSTM checkpoint 在与训练集一致的切换序列上实际输出；测试只要求其满足预先设定的 Top-1/Top-2 与冷概率门。

### 4.2 每个 GUI scope 内的受控工作集

fixture 与对应真实 GUI 应用使用相同 App ID、位于对应 GUI scope，不再出现“测量 fixture 在一个 cgroup、内核回收另一个 cgroup”的问题。

| 应用 | profile | 文件页 | 匿名页 | 目的 |
|---|---|---:|---:|---|
| Firefox / Thunderbird / VLC | 热对照 | 各 64 MiB | 各 64 MiB | 检验是否误回收热工作集 |
| GIMP | ANON_HEAVY | 32 MiB | 256 MiB | 验证高 swappiness 下的匿名冷页选择 |
| ImageViewer | MIXED | 160 MiB | 160 MiB | 验证中性页面类型策略 |
| Evince | FILE_DIRTY | 320 MiB 脏文件 | 32 MiB | 验证脏文件 profile 与受控写回路径 |
| LibreOffice | FILE_CLEAN | 512 MiB | 32 MiB | 验证干净文件页倾向 |
| Solitaire | FILE_CLEAN | 512 MiB | 32 MiB | 第二个文件页冷应用 |

冷应用 fixture 总量为 **2048 MiB**。冷 scope 按 `ANON_HEAVY → MIXED → FILE_DIRTY → FILE_CLEAN` 交错创建；这避免固定 cgroup 创建顺序使 1920 MiB 目标在扫描到 MIXED 前就结束。该顺序不改变 LSTM 输入、概率、页面布局或压力大小，三组完全相同。

### 4.3 压力如何产生

1. 记录压力前 cgroup snapshot 与 fixture 的 `mincore` 驻留页数；
2. 把实验 slice 的 `memory.max` 设置为“当前使用量 + 512 MiB”；
3. 在同一 slice 中启动压力进程，申请 **2432 MiB**；
4. 因可用 headroom 仅 512 MiB，系统必须回收约 **1920 MiB** 才能让压力进程达到 HOLDING；
5. 压力保持时再次记录 `mincore`、`memory.stat`、`memory.events`、`io.stat`、PSI 及 tracepoint 数据；
6. 关闭压力并清理所有 scope。

该设计的优点是回收量由 cgroup 边界决定，而不是依赖某次机器整体内存恰好有多空闲。

## 5. 有效性门：什么情况下结果会被拒绝

每一轮不是“脚本正常退出就算成功”。以下任意失败都会标记 INVALID，并从统计中剔除：

| 门 | 要求 |
|---|---|
| GUI 门 | 8 个应用窗口已出现，PID、窗口类和 systemd scope 匹配 |
| LSTM 门 | 固定 5 步历史、当前 VLC、Firefox Top-1、Thunderbird Top-2、冷应用概率低于门限 |
| V3 门 | Apply 组必须成功写入 `/dev/myfs`，绑定数足够，目标 scope profile 类别完全匹配 |
| 页面画像门 | ANON_HEAVY / FILE_CLEAN / FILE_DIRTY / MIXED 均满足 `memory.stat` 的匿名、文件、脏页比例约束 |
| 压力门 | 压力进程确实到达 HOLDING，边界与 swap 限额正确 |
| 内核动作门 | Bin 组必须有 `policy_hits` 与 `subtree_selected`；workload 组还必须有每一类 profile 的非零 reclaim pass 且 profile miss 为 0 |
| 安全门 | 内存可用量、PSI full 和超时不越过安全阈值 |

最终三组各获得 **3/3 VALID**，种子为 `20260830`、`20260831`、`20260832`。

## 6. 结果一：LSTM bin-reclaim 的选择是否有效

下表是三轮平均值 `[中位数]`：

| 指标 | Linux Native | Bin + LSTM | Bin + LSTM + workload |
|---|---:|---:|---:|
| 冷应用回收来源占比 | 81.00% [80.59] | 99.35% [99.33] | **99.96% [99.95]** |
| 热应用 memory drop | 325.38 MiB [322.31] | 12.54 MiB [13.02] | **0.81 MiB [0.88]** |
| 热 fixture 的精确逐出量 | 11.00 MiB [1.51] | **0 MiB** | **0 MiB** |
| 冷 fixture 的精确逐出量 | 974.00 MiB [1024.00] | 1131.97 MiB [1132.00] | 991.11 MiB [991.01] |

解释：

- Native 的回收约 19% 来自热应用；
- 开启 bin+LSTM 后，约 99.35% 回收来自预测冷应用，热 fixture 三轮均未被逐出；
- workload-aware 把这个选择进一步推到约 99.96%。

这是一条直接的机制证据：LSTM 输出不是只记录在 CSV，而是改变了实际 cgroup reclaim 来源。

## 7. 结果二：四类 workload profile 是否真的被执行

workload-aware 三轮全部满足：ABI-v3 成功、5 个目标绑定有效、`workload_profile_misses=0`。平均内核 pass 与 exact-fixture 结果为：

| profile | 平均内核 reclaim pass | 精确 fixture 逐出表现 | 含义 |
|---|---:|---|---|
| ANON_HEAVY | 3338 | 约 256 MiB anon，主类型约 89% | 匿名冷页被优先处理 |
| FILE_CLEAN | 500 | 约 78 MiB file、32 MiB anon，file 约 71% | 文件页倾向，不是强制排他 |
| FILE_DIRTY | 907 | 320 MiB file、0 anon，file 100% | 脏文件 fixture 已实际逐出 |
| MIXED | 1993 | 160 MiB file、约 114 MiB anon | 中性画像同时处理两类页 |

每轮平均扫描约 **754431 页**、回收约 **492756 页**。四类 pass 均非零，因而不存在“profile 被下沉但内核从未用到”的情况。

`writepage_promotions=0` 并不表示 FILE_DIRTY profile 无效：Evince 的 profile 已被命中且 320 MiB 脏文件页被逐出。该计数为零表示这些 direct-reclaim pass 的原始 `scan_control` 已允许 writeback，所以没有发生“从禁用改为启用”的额外 promotion。若要量化 `allow_writepage` 本身的边际价值，需要单独构造初始 writeback 禁止的对照。

## 8. 结果三：fault、swap、direct reclaim 与 PSI

| 指标 | Linux Native | Bin + LSTM | Bin + LSTM + workload |
|---|---:|---:|---:|
| `pgfault` | 629469 [626721] | 626550 [626633] | 626666 [626593] |
| `pgmajfault` | 4585 [2152] | **1463 [1478]** | 1949 [1918] |
| `workingset_refault_anon` | 5016 [2461] | **1539 [1547]** | 2216 [2047] |
| `pswpin` | 5007 [2454] | **1538 [1546]** | 2213 [2045] |
| `pswpout` | 167399 [154584] | **121140 [120974]** | 260770 [260850] |
| `pgscan_direct` | 660931 [640726] | **609438 [609510]** | 736579 [736525] |
| PSI some | 1615.05 ms [1495.53] | 4221.76 ms [1546.35] | 1932.60 ms [1756.54] |
| PSI full | 1590.59 ms [1468.16] | 3786.71 ms [1503.37] | 1900.56 ms [1721.90] |

### 应当怎样理解这些数值

1. `pgfault` 基本不变，**不能据此说没有收益**。本场景的约 62.6 万次 fault 主要来自固定 fixture 建立和压力进程分配，本来就不是“压力后重返用户工作集”的指标。
2. Bin+LSTM 的 major fault、匿名 refault、swap-in 和 direct scan 的中位数均低于 Native，说明“优先回收预测冷应用”具有正向信号。
3. Native 的 file-refault 与 major-fault 有单轮大值，样本仅三轮，不能把均值百分比当作确定交付收益；报告中必须同时保留中位数与每轮原始数据。
4. workload-aware 相对 bin-only：匿名 refault、swap-in、swap-out、direct scan 与 PSI 中位数都增加。它的选择更精确，但 `ANON_HEAVY` 的较高 swappiness 和第一轮压力使匿名页回收更激进，当前参数存在过度回收。
5. 当前所有 reclaim 都主要体现为 `pgscan_direct`，这是因为压力进程在其 cgroup 内分配内存时同步触发 direct reclaim；这正是本场景用来放大差异的压力路径，不代表普通桌面空闲期的 kswapd 行为。

PSI full 的逐轮值（ms）为：

```text
Native:          648 / 1468 / 2656
Bin + LSTM:     1503 / 8736 / 1121
Workload-aware: 1661 / 2319 / 1722
```

Bin-only 第二轮有 8736 ms 的 PSI full 离群值；workload-aware 没有这个极端值，但其 PSI 中位数仍高于 Native 与 bin-only。因此，当前不能把 PSI 解释为 workload-aware 的收益。

## 9. 当前可以和不可以对外说明什么

### 可以说明

1. 常驻 service 的 GUI 事件、LSTM、cgroup 画像和 `/dev/myfs` V3 下沉链路已在真实应用 scope 中闭环。
2. LSTM 预测驱动的 bin-reclaim 已在三轮中稳定将回收来源集中到预测冷应用，并保护了预测热应用 fixture。
3. workload-aware 的四类 profile 已经真实进入内核 reclaim pass，且页面类型逐出方向符合预期。

### 不可以说明

1. 不能说“workload-aware 已降低真实 PC 的 PageFault、refault 或卡顿”。数据没有支持这一结论。
2. 不能把压力阶段总 `pgfault` 当成用户重新打开/切回应用时的缺页指标。
3. 不能因为 Native 的单轮异常值而对 major-fault 或 file-refault 宣称确定的百分比收益。
4. 不能把“每类 profile 的 pass 非零”表述成“`allow_writepage` 的边际效果已量化”。

## 10. 下一步应验证什么

当前 workload-matrix 应保留为“机制与回收来源”证据。下一场景应专门验证用户收益：

1. 压力只需由预测冷应用的冷页满足，热应用 fixture 必须保持驻留；
2. 解除压力后，固定切回预测热应用，并显式执行多轮 `TOUCH_FILE` 与 `TOUCH_ANON`；
3. 只在这段重访窗口计数 `pgfault`、`pgmajfault`、`workingset_refault_file`、`workingset_refault_anon`、`pswpin`、每次触摸延迟和 PSI；
4. 先比较 Linux Native 与 Bin+LSTM；只有 workload-aware 的 PSI/swap-in 不劣于 bin-only 时，再让它参加性能比较；
5. 每个组至少增加到 5–10 有效轮，并报告逐轮值、中位数和置信区间。

这一步完成后，才能把“回收来源优化”升级为“真实 PC 重访体验优化”的可交付证据。

## 11. 原始证据位置

- 配置：[parp-workload-matrix-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-workload-matrix-config-lzx.json)
- 运行器：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)
- Native 原始三轮：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/native_kernel-20260830_120702-6.17.13-native-6.17.13`
- Bin+LSTM 原始三轮：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/bin_lstm-20260830_115639-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- workload-aware 原始三轮：`/home/lzx/Desktop/PARP/test/outputs/workload_matrix/bin_workload_lstm-20260830_120040-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin`
- 简明数据报告：[native-vs-r11-bin-workload-matrix-20260830-lzx.md](/home/lzx/Desktop/PARP/test_reports/native-vs-r11-bin-workload-matrix-20260830-lzx.md)
