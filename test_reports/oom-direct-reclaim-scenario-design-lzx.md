# PARP OOM 与 Direct Reclaim 配对实验设计

日期：2026-08-19  
目标内核：Linux 6.17.13 / PARP v4.2 及后续同源版本

## 1. 结论

OOM 与 direct reclaim 应拆成三个场景，不能用一个“无限申请内存直到被杀”的场景同时计算全部改善率：

1. `OOM-TRIGGER`：保证测试 cgroup 至少发生一次 OOM，用于验证采集链路，并测量 OOM 发生前可承载的内存和时间。
2. `OOM-THRESHOLD`：在基线临界点附近使用有界负载，比较 OFF 与 APPLY 的 OOM 轮次率和应用存活率。这一场景才用于计算“OOM 降低百分比”。
3. `DIRECT-RECLAIM`：保持在 OOM 以下，持续触发同步 memcg reclaim，比较扫描量、回收延迟和每次应用操作的回收成本。

强制 OOM 场景必然产生一次 OOM，因此不能直接用它的 `oom_kill=1` 和另一组的 `oom_kill=1` 计算优化率。

## 2. 安全隔离

测试进程全部放入独立的 `parp-oom-experiment.slice`，采集器和看门狗必须位于该 cgroup 外。

32 GiB 虚拟机上的正式默认值：

| 属性 | OOM-TRIGGER / OOM-THRESHOLD | DIRECT-RECLAIM |
|---|---:|---:|
| `MemoryMax` | 8 GiB | 8 GiB |
| `MemoryHigh` | `infinity` | 6.4 GiB |
| `MemorySwapMax` | 0 | 512 MiB |
| 宿主 `MemAvailable` 停止线 | 12 GiB | 12 GiB |
| 最长单轮时间 | 120 秒 | 180 秒 |

只有测试 cgroup 的 `memory.events.local:oom_kill` 增长是预期事件。宿主 `/proc/vmstat:oom_kill` 增长、采集器死亡或测试 cgroup 之外出现 OOM 都必须判为无效轮次。

## 3. 工作负载组成

使用六个免登录、可自动化应用：Firefox、LibreOffice、VLC、GIMP、Thunderbird、Files。所有输入使用本地固定文件，禁止网络内容改变负载。

每个应用保持独立子 cgroup，并附带与该应用绑定的内存 fixture。初始总驻留目标为测试 cgroup 上限的约 70%：

| 内存类型 | 目标 | 用途 |
|---|---:|---|
| 可回收冷文件页 | 3.2 GiB | 为 MGLRU/effective-tier 提供可选择的冷页 |
| 热文件页 | 0.8 GiB | 模拟即将复用、应受保护的页面 |
| 应用匿名页 | 1.2 GiB | 模拟应用状态 |
| 压力匿名页 | 从 0 逐步增加 | 产生同步回收并逼近 OOM |
| 真实 GUI 应用 | 实际占用 | 保留真实启动、切换和存活行为 |

自动化按照固定 seed 在六个应用间切换。每步执行“切换窗口—验证前台—触摸该应用热页—执行一次本地操作—等待”，OFF 与 APPLY 必须重放同一份 scenario plan。

## 4. 场景 A：OOM-TRIGGER

目的：证明 OOM 指标能够被稳定采到，并测量在固定 cgroup 配额下的 OOM 临界点。

步骤：

1. 设置 `MemoryMax=8 GiB`、`MemoryHigh=infinity`、`MemorySwapMax=0`。
2. 启动六个应用和固定内存 fixture，完成 30 秒热身。
3. 每 250 ms 增加 64 MiB 匿名页，并逐页写入确保实际驻留。
4. 压力进程使用 `oom_score_adj=1000`，保证优先杀掉压力进程而不是桌面或采集器。
5. 看门狗从 cgroup 外持续读取 `memory.events.local`；检测到首个 `oom_kill` 后再采集 10 秒，然后停止本轮。
6. 若分配达到 9.5 GiB 或 120 秒仍无 cgroup OOM，则判为 `OOM_NOT_TRIGGERED`。

每轮要求：测试 cgroup `oom>=1`、`oom_kill=1`，宿主 OOM 增量为 0。记录首个 OOM 时的已分配字节、`memory.peak`、从 ramp 开始到 OOM 的时间以及 victim PID/进程名。

采集时以目标 cgroup 的 `memory.events.local` 为 OOM 次数真值，并同时启用 `oom:mark_victim` 记录 victim。只看内核日志或只看自动化进程退出码都不足以形成可配对指标。

## 5. 场景 B：OOM-THRESHOLD

目的：计算可交付的 OOM 改善率。

先在 OFF 模式运行 5 次 `OOM-TRIGGER`，取得首个 OOM 的压力字节数中位数 `B50`。随后使用四个有界压力档位：

- `B50 - 384 MiB`
- `B50 - 256 MiB`
- `B50 - 128 MiB`
- `B50`

每个档位只分配到指定值，保持 60 秒并继续固定应用切换，不再无限增长。每个档位做 10 轮 OFF/APPLY 配对实验。

主指标：

- `OOM 轮次率 = 发生 oom_kill 的轮数 / 有效轮数`
- `OOM 降低率 = (OFF OOM轮次率 - APPLY OOM轮次率) / OFF OOM轮次率 × 100%`
- `应用存活率 = 结束时仍存活的受控应用数 / 6`
- `成功操作数/轮` 和 `oom_kill/100 次操作`
- `P90 无 OOM 承载量`：至少 90% 轮次不发生 OOM 的最大压力档位

若 OFF 在所有档位均为 0 次 OOM，应将档位整体上移 128 MiB；若 OFF 在所有档位均为 100% OOM，应整体下移 128 MiB。不得根据 APPLY 结果重新选择档位。

## 6. 场景 C：DIRECT-RECLAIM

目的：在不发生 OOM 的情况下稳定产生同步回收，并评价优化对前台停顿的影响。

步骤：

1. 设置 `MemoryHigh=6.4 GiB`、`MemoryMax=8 GiB`、`MemorySwapMax=512 MiB`。
2. 建立与 OOM 场景相同的冷热页和六应用状态。
3. 压力页以每 500 ms 32 MiB 的速度增长到 7.6 GiB 总驻留目标，然后保持 60 秒。
4. 保持固定 seed 的应用切换和热页复用；本场景不允许 `oom_kill`。

主指标分成两类，避免继续把不同内核路径混为一谈：

- 同步 memcg reclaim：`mm_vmscan_memcg_reclaim_begin/end` 次数、总时长、P95/P99、最大时长。
- direct 页统计：`pgscan_direct`、`pgsteal_direct`、回收效率、direct scan share。

归一化指标使用“每 100 次成功应用操作”，不能只比较一轮总量：

- `direct scan pages / 100 ops`
- `direct reclaim time / 100 ops`
- `memcg reclaim P95/P99`
- `pgsteal_direct / pgscan_direct`

`mm_vmscan_direct_reclaim_begin/end` 只代表全局 direct reclaim。该场景受 `memory.high` 控制时，预期主要出现 memcg reclaim；全局 direct trace 为 0 不等于同步 direct reclaim 为 0。

## 7. 可选场景 D：GLOBAL-DIRECT-RECLAIM

如果验收明确要求 `mm_vmscan_direct_reclaim_begin/end` 本身必须非零，还需要单独做全局低水位场景。它不能与受 `memory.high` 控制的场景混用。

在专用 32 GiB 虚拟机中设置测试 cgroup `MemoryHigh=infinity`、`MemoryMax=29 GiB`、`MemorySwapMax=0`，使用 8 个并发匿名页分配器以合计约 1 GiB/s 的速度爬坡。采集器和硬看门狗位于 cgroup 外；满足以下任一条件立即停止压力进程：

- 已采到至少 100 对 `mm_vmscan_direct_reclaim_begin/end`；
- 宿主 `MemAvailable < 1.5 GiB`；
- 宿主 OOM 计数增加；
- 运行达到 90 秒。

该场景只用于比较全局 direct reclaim 次数、总时长、P95/P99 和 scanned pages，不用于 OOM 降低率。宿主 OOM 或 trace begin/end 不配对时整轮无效。正式运行前应保留 VM 快照，并确认 VM 中没有其他业务进程。

## 8. 配对方法

每个压力档位和 seed 都生成一次不可变 scenario plan，保存 SHA-256。OFF 与 APPLY 必须满足：

- 相同 VM 内存、CPU、swap 和 cgroup 配额；
- 相同应用、输入文件、操作序列和等待时间；
- 相同 seed、fixture 大小和压力增量；
- 每轮前清理上轮进程并验证 cgroup 为空；
- 交替采用 OFF→APPLY、APPLY→OFF，减少固定顺序偏差；
- 至少 10 对有效轮次；无效轮次不能进入均值。

分别比较 Native/OFF、Tier2-only、effective-tier-only 和 Combined。先报告每一对的差值，再报告均值、P50/P95 和 95% bootstrap 置信区间。

## 9. 1 GiB 校准结果

已在当前 `6.17.13-parp-lzx-v4.2+` 内核上完成一次小规模链路校准：

- cgroup：`MemoryMax=1 GiB`、`MemoryHigh=infinity`、`MemorySwapMax=0`；
- 压力目标：约 1.25 GiB 匿名页；
- 约 3 秒后 systemd 返回 `result=oom-kill`；
- `memory.events`：`max=24`、`oom=1`、`oom_kill=1`；
- 内核日志明确记录 `constraint=CONSTRAINT_MEMCG`，victim 为压力进程 `python3`，不是宿主进程；
- trace：memcg reclaim begin/end 各 24 次；
- `pgscan_direct=9`、`pgsteal_direct=4`；
- 全局 direct-reclaim trace 为 0，符合受限 cgroup 的内核路径语义。

校准还发现：当 `MemoryHigh=768 MiB`、`MemoryMax=1 GiB` 时，单个匿名分配进程会在 high 线上被长时间节流，原有 PSI guard 会先终止实验。因此正式 OOM 触发场景使用 `MemoryHigh=infinity`；direct-reclaim 测量另设独立场景。

## 10. 报告判定

`OOM-TRIGGER` 的一次预期 cgroup OOM 是有效结果，不应被测试框架当成自动化失败。`OOM-THRESHOLD` 中的 cgroup OOM 是被比较的结果。任一场景出现宿主 OOM、trace 丢失、scenario plan 不一致、采集器退出或应用操作序列未完成，整轮无效。
