# r8 大量页面复用与预测冷应用增强回收实现记录

日期：2026-08-28  
状态：代码、测试场景、编译和安装完成；`#37` 启动审计后补齐 cgroup 回收路径，等待启动 `#38` 采集正式数据。<!-- lzx-note -->

## 验证目标

在 LSAPP 训练序列能给出正确冷热应用排序的前提下，构造“预测冷应用的冷页容量足以独立承担全部回收目标”的场景，并分别测量文件页 refault、匿名页 refault/swap-in、内存 PSI 停顿和逐页复用延迟。正式对比为：

1. pristine Linux 6.17.13 Native；
2. r8 仅 LSTM reclaim-bin 排序；
3. r8 LSTM reclaim-bin 排序 + 预测冷应用首轮增强回收。

## 场景

- 八个免登录 GUI 应用按照训练序列 `Thunderbird → Firefox → Thunderbird → Firefox → VLC` 切换。
- GUI 与受控页面 worker 同处各自的 `automation-<app>.scope`，因此 runtime service 经 `/dev/myfs` 下沉的 app/cgroup 绑定同时覆盖 GUI 和受控页面。
- 五个预测冷应用共有 1920 MiB 受控冷页；回收目标 768 MiB，容量比 2.5，理论上无需回收预测热应用即可满足压力。
- 压力稳定后，Firefox 和 Thunderbird 分别执行：完整文件页首次复用、完整匿名页首次复用、文件/匿名页第二次热复用。
- 文件数据逐页真实落盘并 `fsync`，匿名页使用 `MAP_PRIVATE|MAP_ANONYMOUS` 私有映射并逐页写入；复用阶段逐页访问完全相同的地址，不再以 UI 滚动替代页面复用。每轮文件位于独立 `run_dir`、使用唯一 inode，避免 page-cache memcg 计费跨轮复用。<!-- lzx-note -->

配置：`test/test/parp-page-reuse-config-lzx.json`  
runner：`test/test/parp-real-pc-experiment-lzx.py`  
同 scope 启动器：`test/automation/launch_app_with_reuse_lzx.py`

## 指标口径

| 阶段 | 核心指标 | 含义 |
|---|---|---|
| 文件页首次复用 | `workingset_refault_file`、`pgfault`、`pgmajfault` | 文件页被回收后重新进入 page cache，以及访问是否需要存储 I/O |
| 匿名页首次复用 | `workingset_refault_anon`、`pswpin`、`pgfault`、`pgmajfault` | 匿名页被换出后重新换入，以及恢复时的缺页代价 |
| 第二次热复用 | 同上 | 热对照；应接近零新增 refault/swap-in |
| 压力与复用 | cgroup/system PSI `some/full total` | `some` 为至少一个任务因内存压力停顿；`full` 为所有非空闲任务同时停顿，单位为累计微秒 |
| 用户可感知代理 | worker 逐页复用延迟、GUI 切换动作延迟 | PSI 不是 FPS，必须与延迟共同解释 |
| 回收来源 | 每应用 `memory.current`、file、anon 降幅和冷热占比 | 验证是否主要从预测冷应用取得目标页 |

## 新内核策略

编译开关：`CONFIG_PARP_RECLAIM_COLD_AGGRESSIVE`，默认 `n`。  
运行时开关：`vm.parp_reclaim_cold_aggressive_enabled`，默认 `0`。  
阈值：内核通用默认值为 `vm.parp_reclaim_cold_bin_max=2`；本次 LSAPP 精确复用实验使用 `0`。八个 bin 按 Q15 概率等宽映射，bin 1 会覆盖约 13% 概率、排名第 2 的 Thunderbird；而实验对预测冷应用的约束是概率不高于 1%，因此只能增强 bin 0，避免误回收即将复用的 Thunderbird。<!-- lzx-note -->

策略只接受有效 `/dev/myfs` 应用预测支持的叶 cgroup；无应用绑定、过期预测和父水位 fallback 都不会触发增强。bin 0/1/2 在当前 memcg 首次访问时分别降低 3/2/1 级 reclaim priority（约 8x/4x/2x 扫描预算），将 swappiness 至少提高到 140/120/100，并允许脏页写回。扫描完当前 memcg 后立即恢复原始值，不把增强压力泄漏给热应用或无关 scope。策略同时接入全局 MGLRU `shrink_many()` 和 `memory.max` 等目标 cgroup 回收使用的 `shrink_node_memcgs()`；后者是在 `#37` 干净启动后的正式实验前审计中补齐，避免 cgroup 场景静默绕过策略。<!-- lzx-note -->

动作证据位于只读 `reclaim_cold_stats`：`passes`、`scanned`、`reclaimed`、各 bin pass、priority boost、writepage/swappiness promotion。预测和绑定仍只通过 `/dev/myfs` 写入。

## 构建与安装

- 镜像：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r8-cold-pressure`
- 构建号：`#38`
- APPLY、开关关闭、PARP 整体关闭三种编译路径均通过。
- Python 语法检查、132 动作 dry-run、8 MiB 文件/匿名逐页回放自测通过。
- `test/test/run-tests-lzx.sh -v`：55/55 通过。
- v4.2 完整补丁 SHA-256：`c9ede208038c912656de611eb053cc3cc2ffff34a58144a11c870de72efe7d57`；已在固定 Native commit `6609c4d49ebe...` 上通过 `git apply --check`。
- r8 已安装到 `/boot` 和 `/lib/modules`，并设为下一次单次启动项；当前长期默认 r7 未覆盖，待干净启动验证后再切换。

## 下一步

重启进入 r8 后先验证新 sysctl、`/dev/myfs`、runtime service 和启动日志；随后以相同 seed 在 r8 连续运行 bin-only 与 bin+cold-pressure 各三轮。再启动 Native 执行三轮，最后回到 r8 汇总配对报告。<!-- lzx-note -->

## 正式运行前校准发现

首次各三轮校准发现 Python 的 `mmap(-1, access=ACCESS_WRITE)` 在 Linux 上形成共享匿名映射，cgroup-v2 将其计入 `shmem/file`，不能作为匿名页 refault 的正式证据。这六轮已明确降级为校准轮次。fixture 已切换为 `MAP_PRIVATE|MAP_ANONYMOUS`；正式结果还要求每个应用在压力前实际计费的 `anon` 和扣除 `shmem` 后的磁盘文件页都达到配置容量的 95%，否则轮次自动判为 `INVALID`。<!-- lzx-note -->

校准目录（不得进入正式汇总）：

- `bin_lstm-20260828_201448-...`
- `bin_cold_lstm-20260828_201847-...`

## r8 正式中间结果

正式目录：

- bin-only：`test/outputs/page_reuse/bin_lstm-20260828_202427-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r8-cold-pressure`
- bin + cold-bin0：`test/outputs/page_reuse/bin_cold_lstm-20260828_202743-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r8-cold-pressure`

两组 seed 均为 `20260828/20260829/20260830`，共六轮全部 `VALID`；每轮八个应用均通过私有匿名页和独立磁盘文件页的 95% 计费容量门。以下为三轮中位数：

| 指标 | bin-only | bin + cold-bin0 | 初步变化 |
|---|---:|---:|---:|
| 预测冷应用回收来源占比 | 98.330% | 99.839% | +1.509 个百分点 |
| 预测热应用被回收 | 12.984 MiB | 1.250 MiB | -90.37% |
| `pgscan` | 305,163 | 326,286 | +6.92% |
| `pgsteal/pgscan` | 64.472% | 60.667% | -3.805 个百分点 |
| `pswpout` | 108,132 | 138,782 | +28.34% |
| 压力阶段 cgroup PSI some | 842,138 us | 841,482 us | -0.08%，基本不变 |
| 热应用精确复用 file refault | 0 | 0 | 无额外收益 |
| 热应用精确复用 anon refault / swap-in | 0 / 0 | 0 / 0 | 无额外收益 |
| 冷增强实际回收页 | 0 | 197,948 | 机制已动作 |

当前只能得出 r8 内部消融结论：cold-bin0 确实把回收来源进一步压向预测冷应用，显著减少热应用的非目标页回收；但 bin-only 已足够保护本场景的全部精确复用目标，因此 refault/swap-in 存在零值天花板，cold-bin0 没有继续改善终端缺页指标，反而增加扫描与换出。首次复用时间虽从 40,917 us 降到 16,129 us，但两组按块顺序执行，可能受 CPU/cache 顺序效应影响，在 Native 配对完成前不作为收益结论。<!-- lzx-note -->

下一步启动 pristine Native `6.17.13-native-6.17.13`，使用完全相同 seed 和场景运行三轮；最终报告只比较正式目录，并将 Native 结果与上述两组并列。<!-- lzx-note -->
