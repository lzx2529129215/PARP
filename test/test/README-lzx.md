# PARP / MGLRU 验收实验

本目录实现当前 r9 Shadow 内核的诊断基线，以及后续同源 Native/OFF 与 Apply 内核的成对验收。当前 r9 的 `apply_compiled=0`，因此首轮结果只能标记为 `DIAGNOSTIC_BASELINE`，不能宣称 PageFault 或峰值异常已经降低。

当前基线数值、验收目标换算、结果限制和复现实验说明见 [`baseline-results-lzx.md`](baseline-results-lzx.md)。

新一轮应用集合、LSTM重训、两个独立开关、精确重放和OOM校准的完整方案见 [`实验设计-lzx.md`](实验设计-lzx.md)。主基线固定为 Linux 6.17.13，唯一修改与编译源码树为 `lzx/kernel/src/linux-6.17.13-parp-lzx`。<!-- lzx-note -->

两套完整实验结束后，可以生成包含各轮原始值、均值、标准差、极值和目标阈值的醒目对比报告：

```bash
python3 test/baseline-report-lzx.py \
  --hotcold <hotcold输出目录>/summary.json \
  --peak <peak输出目录>/summary.json \
  --output-dir <合并报告目录>
```

## 实验口径

- 冷热识别：WPS、Files、QQ；总受控逻辑内存为物理内存的 150%，启用 swap，按 seed 生成随机但可重放的窗口切换序列；完整模式运行 10 轮。
- 峰值调度：WPS、Files、QQ、Firefox、GIMP、LibreOffice；日常内存比例合计 65%，并发峰值比例合计 125%，任一应用峰值不超过物理内存；先建立峰值压力，再连续发出 6 个应用启动并验证窗口，每轮至少 100 个有效步骤，完整模式运行 3 轮。
- PageFault 主采集来自 `exceptions:page_fault_user` tracepoint，并只过滤受控应用内存 sidecar PID；测试 slice 的 `pgfault/pgmajfault` 用于包含真实 GUI 应用的交叉复核。
- 真实refault按每轮测试cgroup `memory.stat` 的首尾差值统计，分别报告 `workingset_refault_file` 与 `workingset_refault_anon`；禁止用未来访问标签代替真实refault。
- 完整回收诊断同时记录 `workingset_activate/restore`、`pgscan/pgsteal`、direct/kswapd扫描回收量、扫描效率、direct/memcg reclaim延迟和kswapd CPU时间。旧基线没有采集的字段显示为 `N/A`，不能填0。
- 当前 `schema-v3` 已吸收同目录 `memsched_exp` 独立测量核心中的严格检查：cgroup 首尾路径与 device/inode 必须一致，`memory.stat`、`memory.events`、`cpu.stat`、`io.stat` 和 `memory.current` 必须可读，必需计数器不得缺失或倒退；任一条件失败都使该轮无效，原始文件仍保留。
- trace 除 ring 丢失外，还检查 direct reclaim 与 memcg reclaim 的 begin/end 嵌套、孤立 end、未闭合 begin 和关键事件解析失败；配对错误不会被静默丢弃，也不会用成功配对数掩盖。
- CPU/I/O 使用同一个测试 slice 的 `cpu.stat` 与 `io.stat` 首尾差分，报告 CPU 总时间、单核等价占比、整机占比、块层读写量和吞吐；页缓存命中的读取不计入块层 I/O。
- 每个应用报告“启动动作开始到匹配 X11 窗口验证成功”的就绪代理延迟，并输出轮内均值与 P95。该值不是首个可交互帧；峰值场景先并发启动再逐个验证，因此应视为启动就绪上界。
- 每次实验根目录写出 `system-metadata-lzx.json`，记录内核 release/config 哈希/命令行、CPU、内存、swap、VM sysctl、THP、CPU governor、X11 会话和结果文件系统，用于检查 OFF/Apply 环境是否同源。
- OOM必须拆分为测试cgroup `oom`、测试cgroup `oom_kill` 和宿主 `oom_kill`；前两项用于说明测试边界，宿主OOM会使该轮立即无效。
- trace ring 固定为每 CPU 1 MiB 并持续流式读取；任一 `overrun/commit overrun/dropped events` 非零都使该轮无效，避免大 ring 自身污染内存压力。
- 峰值异常总数为自动化动作/启动失败、低内存窗口命中和测试 cgroup `oom_kill` 的合计。宿主 `oom_kill` 不计入成绩，而是立即中止并判该轮无效。

随机不等于不可复现：每轮先由 seed 生成序列并保存 `scenario.json`，优化前后必须复用完全相同的 seed 和场景。正式改善率为 `(基线均值 - 优化均值) / 基线均值 * 100%`。

现在每轮还会保存不含临时路径的 `scenario-plan.json`。Apply 侧必须用 `--replay-from <Native输出目录>` 重放逐步应用、冷区偏移、停留时间及OOM burst设置；`paired-report-lzx.py` 会逐轮校验计划哈希和系统元数据，不能只凭seed相同认定为有效配对。

## 已实现并实际执行的自动化场景

### 场景一：三应用冷热随机切换

该场景已经在 r9 Shadow 内核上完整执行 `10` 轮，每轮 `24` 个计分步骤，共 `240` 步。基准 seed 为 `20260812`，各轮实际 seed 为 `20260812～20260821`。

参与应用及无外部副作用的操作如下：

| 应用 | 启动与窗口识别 | 每次切换后的UI操作 |
|---|---|---|
| WPS | 启动 `wps`，匹配WPS/Writer窗口 | `Page_Down` |
| Files | 在仓库目录打开Nautilus窗口 | `Page_Down` |
| QQ | 启动Linux QQ并匹配QQ窗口 | `Tab` |

每轮自动化顺序为：

1. 启动WPS、Files和QQ，并确认三个窗口都已出现。
2. 为每个应用启动独立内存sidecar。三者受控逻辑内存合计为物理内存的 `150%`，每个应用约占三分之一；其中约 `2%` 为匿名内存，其余为稀疏文件映射。
3. 依次执行 `PREPARE`，访问完整映射，先建立驻留、回收和swap压力。
4. 将PageFault trace过滤到三个sidecar PID，然后才开启正式计分区间，避免把初始化缺页混入窗口切换指标。
5. 使用固定seed随机选择下一个应用，并禁止连续两步选择同一应用。
6. 切换并置顶目标窗口，验证前台窗口确实属于目标应用。
7. 访问目标应用约 `1%` 的热区；每一步另有 `35%` 概率随机访问约 `0.5%` 的冷区。
8. 发送表中的UI按键，随机停留 `0.4～1.2` 秒，在trace中写入步骤开始/完成标记。
9. 完成24步后关闭trace、sidecar和应用，保存该轮结果并进入下一轮。

该场景的正式验收指标是受控sidecar的 `exceptions:page_fault_user`。GUI应用与sidecar的总体 `pgfault/pgmajfault`、direct reclaim、kswapd、PSI、swap和cgroup事件作为交叉复核。

### 场景二：六应用并发峰值与持续切换

该场景已经在 r9 Shadow 内核上完整执行 `3` 轮，每轮 `100` 个计分步骤，共 `300` 步。基准 seed 为 `20260812`，各轮实际 seed 为 `20260812～20260814`。

参与应用及操作如下：

| 应用 | 峰值逻辑内存占物理内存 | 每次切换后的UI操作 |
|---|---:|---|
| WPS | 22% | `Page_Down` |
| Files | 12% | `Page_Down` |
| QQ | 16% | `Tab` |
| Firefox | 27% | `Ctrl+L` |
| GIMP | 23% | `+` |
| LibreOffice Writer | 25% | `Page_Down` |

六个应用日常内存比例合计为 `65%`，并发峰值比例合计为 `125%`。每轮自动化顺序为：

1. 创建Firefox隔离profile和GIMP本地测试图片，避免依赖网络或用户文档。
2. 为六个应用分别启动内存sidecar并确认控制socket可用。
3. 在启动GUI应用之前对六个sidecar全部执行 `PREPARE`，先形成125%逻辑峰值压力。
4. 连续发出六个应用启动命令，再逐一等待并验证六个窗口，模拟压力已经存在时的应用并发冷启动。
5. 过滤sidecar PID并开启trace，执行100步随机窗口切换；同样禁止连续重复应用。
6. 每一步切换窗口、验证前台、访问约 `2%` 热区；另有 `20%` 概率访问约 `0.4%` 冷区，然后执行表中的UI操作并停留 `0.4～1.2` 秒。
7. 同时持续检测自动化/启动失败、低内存弹窗、测试cgroup OOM和宿主OOM。
8. 完成100步后清理六个sidecar与应用并生成该轮结果。

峰值正式指标为 `启动或自动化失败 + 低内存弹窗 + 测试cgroup oom_kill`。宿主OOM会立即中止并使该轮无效，不能当作普通得分。

### 冒烟场景

两套场景均提供 `smoke` 配置，用于在完整采集前验证窗口识别、trace、cgroup和清理链路：冷热为1轮6步、逻辑内存3%；峰值为1轮12步、逻辑内存5%。冒烟结果只验证链路，不纳入正式基线。

### 当前尚未执行的场景

- Blender渲染、QEMU/KVM虚拟机和Ollama本地大模型仍是扩展建议，尚未接入自动化和正式计分。
- 当前只完成r9 `mode=0`、`apply_compiled=0` 的优化前基线，尚未执行Apply内核的同场景配对实验。
- 当前六应用峰值场景的异常总数基线为0，尚未完成“安全增强到稳定非零异常”的峰值校准，因此暂时不能计算异常总数降低30%的改善率。

## 安全边界

工具只在 `parp-acceptance.slice` 设置有限的 `MemoryHigh` 和 `MemoryMax`，不修改 PARP/MGLRU 模式、swappiness、水位、swap 配置，不调用 `drop_caches` 或 `memory.reclaim`。宿主 `oom_kill` 增加会立即停止。full 保留 `MemAvailable < 2 GiB` 及 4 GiB PSI 联合保护线；smoke 在小内存开发机上使用独立的 512 MiB/768 MiB 保护线，只用于管道验证，不作为正式验收。测试 cgroup 内的 OOM 会被记录，但不会放宽宿主保护线。<!-- lzx-note -->

为获得测试 slice 的 `cpu.stat`/`io.stat`，runner 会用已授权 sudo 对当前用户的 `user-UID.slice` 和 `user@UID.service` 设置运行时 `CPUAccounting=yes`/`IOAccounting=yes`。这只启用当前启动周期的 cgroup controller 计数，不写持久配置，重启后由下一次实验自动重新启用。

预检还要求至少保留 1024 个 inotify watch。大型源码树的 IDE 文件监视器可能耗尽 watch，导致 systemd 无法观察测试 scope 退出并卡在清理阶段；这种情况下工具会在正式运行前阻止采集。

稀疏文件 sidecar 在退出时自动删除；报告、trace 和自动化日志保留在 `test/outputs/parp_acceptance/`。

## 使用方法

先做预检和单轮小规模冒烟：

```bash
cd /home/lzx/Desktop/PARP
bash test/test/run-tests-lzx.sh -v
python3 test/test/parp-acceptance-lzx.py preflight --profile smoke --suite all
python3 test/test/parp-acceptance-lzx.py run --profile smoke --suite hotcold
python3 test/test/parp-acceptance-lzx.py run --profile smoke --suite peak
```

冒烟通过后执行当前内核完整诊断基线：

```bash
python3 test/test/parp-acceptance-lzx.py run --profile full --suite hotcold --seed 20260812
python3 test/test/parp-acceptance-lzx.py run --profile full --suite peak --seed 20260812
```

每次运行首先打印 `output=...`。持续日志为该路径内的 `round-NN/automation.log`、`round-NN/monitor.csv` 和 `round-NN/trace/stream-error.txt`；汇总为 `summary.md` 与 `summary.json`。`round-NN/round-result.json` 中的 `validity.invalid_reasons` 会列出 cgroup 端点、trace 配对、监控样本或启动就绪的具体失效原因；`launch`、`cgroup`和 `system` 分别保存启动延迟、CPU/I/O/回收和宿主差分指标。

## 扩展应用建议

首版不依赖额外安装。正式扩展可加入 Blender、QEMU/KVM + virt-manager、Ollama，分别覆盖图形渲染、虚拟机和本地大模型峰值。新应用必须先补齐启动命令、窗口识别、无外部副作用的 UI 操作、scope 归属和失败检测，未通过预检时只能记为 `NOT_INSTALLED/SKIP`，不能算作成功步骤。

LSAPP 对齐实验不使用上述超大应用，也不再依赖QQ登录。它使用本机已经安装的 Firefox、LibreOffice、VLC、GIMP、Audacity、Thunderbird、Evince、PCManFM/Nautilus 和 GNOME Calculator；所有操作只读取运行时生成的本地 HTML、TXT、WAV、EML、PDF、PPM 或仓库目录。

## 预测冷应用与大量页面复用场景（r8）

`parp-page-reuse-config-lzx.json` 定义了一个机制验证场景：八个免登录 GUI 应用仍按 LSAPP 训练集序列切换，每个应用同时在同一个 `automation-<app>.scope` 中运行受控文件页/匿名页 worker。五个低预测概率应用提供 1920 MiB 冷页面，而目标回收量为 768 MiB，冷池容量是需求的 2.5 倍，因此无需牺牲预测热应用也足以完成回收。<!-- lzx-note -->

压力稳定后，Firefox 和 Thunderbird 各自按“文件页首次精确复用、匿名页首次精确复用、文件/匿名页第二次热复用”执行。分阶段结果记录：

- 文件页：`workingset_refault_file`、`pgfault`、`pgmajfault`；
- 匿名页：`workingset_refault_anon`、`pswpin`、`pgfault`、`pgmajfault`；
- 卡顿代理：测试 slice 和应用 scope 的 PSI `some/full total` 累计停顿微秒，以及 worker 逐页复用延迟；
- 回收来源：各应用 `memory.current`、file、anon 降幅与冷热应用占比；
- 热对照：第二次复用应接近零新增 refault/swap-in，否则本轮存在持续回收干扰。<!-- lzx-note -->

PSI `some` 表示统计窗口内至少一个任务因内存压力停顿，`full` 表示所有非空闲任务同时停顿；它们是内存卡顿时间而不是 FPS，需要与精确复用延迟一起解释。正式配对使用相同配置和 seed 依次运行：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run --config test/test/parp-page-reuse-config-lzx.json --policy native_kernel --scenario page_reuse_cold_only --rounds 3 --seed 20260828
python3 test/test/parp-real-pc-experiment-lzx.py run --config test/test/parp-page-reuse-config-lzx.json --policy bin_lstm --scenario page_reuse_cold_only --rounds 3 --seed 20260828
python3 test/test/parp-real-pc-experiment-lzx.py run --config test/test/parp-page-reuse-config-lzx.json --policy bin_cold_lstm --scenario page_reuse_cold_only --rounds 3 --seed 20260828
```

三组分别是 pristine Native、仅 LSTM bin 排序、LSTM bin 排序加预测冷应用首轮增强回收。后两组使用同一 r8 内核，仅切换 `vm.parp_reclaim_cold_aggressive_enabled`，可直接做运行时消融。<!-- lzx-note -->

## 预测冷应用冷脏文件页场景（r8）

`parp-cold-dirty-config-lzx.json` 为八个 GUI 应用分别建立独立的 mmap-dirty、`MADV_COLD`、resident 文件工作集。压力前后通过 `mincore()` 精确统计这些文件页的 residency，并同时记录每应用 cgroup 写回量、`pgscan/pgsteal` 和 PSI，用于区分“回收来自冷应用”与“具体回收了冷应用脏文件页”。<!-- lzx-note -->

运行期间使用显式 dirty byte 阈值阻止后台 flusher 在压力前随机清洗工作集，runner 在 `finally` 中恢复原始 sysctl。任何应用的 resident 容量低于 95%、dirty 容量低于 80%，或 sysctl/预测/绑定/机制证据缺失，整轮都会判为 `INVALID`。

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-dirty-config-lzx.json \
  --policy bin_lstm --scenario cold_dirty_reclaim \
  --rounds 3 --seed 20260828

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-dirty-config-lzx.json \
  --policy bin_cold_lstm --scenario cold_dirty_reclaim \
  --rounds 3 --seed 20260828
```

## LSTM + workload-aware anon/file reclaim 消融场景（r9）

`parp-workload-matrix-config-lzx.json` 在同一组符合 LSAPP 训练序列的 GUI 切换中，建立匿名页占主导、干净文件页占主导、脏文件页占主导和混合四类预测冷应用；预测热应用只作为保护对照。混合类使用 ImageViewer。workload-aware 模式以原始 LSTM 概率（默认不高于约 1%）而不是 ordinal-rank 决定“预测冷”：只要 ABI-v3 画像有效，该应用就直接进入最早 reclaim bin，因此低概率但排名第七的 ImageViewer 不会被排名地板误排除。该规则只作用于 `bin_workload_lstm`，不改变 Native 或普通 `bin_lstm` 的稳定排名分桶。冷应用的 fixture scope 按 ANON_HEAVY、MIXED、FILE_DIRTY、FILE_CLEAN 交错创建；这只避免固定 cgroup 创建顺序在达到 1920 MiB 目标前遮蔽后面的 MIXED 策略，既不改变 LSTM 分数，也不改变回收目标或对照组。Evince 的 FILE_DIRTY fixture 为 320 MiB：它在最终训练序列和取样间可容忍少量正常后台写回，仍以至少 80% 脏页满足严格画像门。每个 fixture 与其 GUI 应用使用相同 App ID，并处于对应 GUI scope 内，避免采集 scope 与实际回收 scope 分离。当前冷池为 2048 MiB，回收目标为 1920 MiB、压力申请为 2432 MiB；目标严格小于冷池容量，且足以使四类预测冷 cgroup 都获得实际扫描机会。`lzx-note`

服务在切换事件上从每个绑定 cgroup 的 `memory.stat` 形成画像，经 `/dev/myfs` ABI v3 下沉。`bin_workload_lstm` 仅对画像有效且预测冷的 cgroup 采用页面类型倾向：匿名主导为较高 swappiness，干净文件主导为较低 swappiness，脏文件主导允许受控写回，混合负载维持中性；无画像或过期绑定均不改变既有 bin-reclaim。`lzx-note`

使用相同配置、轮数和 seed 做三组消融：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-workload-matrix-config-lzx.json \
  --policy native_kernel --scenario workload_matrix_reclaim \
  --rounds 3 --seed 20260828

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-workload-matrix-config-lzx.json \
  --policy bin_lstm --scenario workload_matrix_reclaim \
  --rounds 3 --seed 20260828

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-workload-matrix-config-lzx.json \
  --policy bin_workload_lstm --scenario workload_matrix_reclaim \
  --rounds 3 --seed 20260828
```

结果必须同时满足：预测/绑定有效、四类 cgroup 画像均已由内核命中且无画像 miss、冷工作集在压力前驻留充足、冷池容量足以覆盖回收目标。该序列要求 Firefox 为 Top-1、Thunderbird 为 Top-2，二者概率均不低于 0.08，且所有预测冷应用不高于 0.01；这保留了至少 8 倍的热/冷概率间隔，避免用启动时长扰动下的绝对概率小幅波动否定排序正确的预测。主指标是各类 fixture 的精确 anon/file 驻留下降和冷/热回收来源分布；交叉指标为 `workingset_refault_{anon,file}`、`pswpin/pswpout`、`pgscan/pgsteal`、脏页写回量以及测试 slice 的 PSI `some/full`。因此它检验的是“在足够的预测冷容量下，按真实页组成选择回收类型”而不是仅仅提高扫描强度。`lzx-note`

## 冷脏页替代热应用干净冷页场景

`parp-cold-dirty-preserve-config-lzx.json` 实现严格的回收替代关系。五个预测冷应用合计具有 480 MiB 干净冷页和 400 MiB 冷脏页，三个预测热应用合计具有 384 MiB 页面级冷、应用级预测热的干净页；回收目标为 768 MiB。容量关系满足 `cold_clean < target <= cold_clean+cold_dirty` 且 `cold_clean+hot_clean >= target`。因此 clean-first 路径可以通过冷干净页加热干净页完成目标，而增强策略可以通过冷干净页加冷脏页完成同一目标。绝对容量可调整，但这两个不等式是配置加载时的硬门禁。<!-- lzx-note -->

每个应用 fixture 使用三个独立文件和 inode：`clean.data` 在 fsync 后 `MADV_COLD`，`dirty.data` 在 fsync 后重新逐页修改且不 flush 再 `MADV_COLD`，小型 `hot.data` 只随真实前台切换访问。为避免后台回写提前清洗实验脏页，最终 VLC 切换和预测下沉前会对五个冷应用再次执行 `REDIRTY`，且不会触碰三个热应用的 `clean.data`。压力前后使用 `mincore()` 分别统计三类页面的驻留下降；压力后依次精确重读 Firefox、Thunderbird、VLC 的 clean 文件，并再次 warm 重读。结果文件 `cold-dirty-preserve-hot-clean-result.json` 同时保存冷干净/冷脏/热干净回收量，以及首次与 warm 复用的 file refault、major fault、PSI 和逐页触碰延迟。<!-- lzx-note -->

使用相同 seed 依次运行三组：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-dirty-preserve-config-lzx.json \
  --policy native_kernel --scenario cold_dirty_preserve_hot_clean \
  --rounds 3 --seed 20260830

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-dirty-preserve-config-lzx.json \
  --policy bin_lstm --scenario cold_dirty_preserve_hot_clean \
  --rounds 3 --seed 20260830

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-dirty-preserve-config-lzx.json \
  --policy bin_workload_lstm --scenario cold_dirty_preserve_hot_clean \
  --rounds 3 --seed 20260830
```

第三组要求 `/dev/myfs` ABI v3 为五个冷应用发布 `FILE_DIRTY` 画像，从而在增加首次扫描预算的同时采用 `swappiness=20` 和 `allow_writepage=true`。一轮实验是否 `VALID` 只由页面状态、容量、预测、绑定、压力和复用取证完整性决定，不以策略必须获益作为有效性条件；这允许实验真实证伪 cold-aggressive 假设。<!-- lzx-note -->

## 第五场景：受限原生写回下的 cold-aggressive 增量验证

`parp-cold-writeback-gate-config-lzx.json` 专门隔离第四场景没有满足的条件：五个预测冷应用只有 120 MiB 干净冷页、但有 640 MiB 冷脏页；三个预测热应用有 576 MiB 随后必须精确复用的干净冷页，目标回收量为 512 MiB。因而冷干净页不足，原生路径若不能及时把冷脏页写回并再次回收，就必须触及热应用干净页。<!-- lzx-note -->

场景在实验期间临时把 `vm.laptop_mode` 设置为 600，使 reclaim 初始进入 `may_writepage=0`；该值在压力前再次取证，并在任何成功或异常退出路径中恢复原值。LSTM 排名和五个 `FILE_DIRTY` 画像通过门禁后，自动化立即再次 `REDIRTY` 五个冷应用；fixture socket 不产生前台事件，所以预测不变，但压力前脏状态的时间窗被压到最短。父实验 cgroup 的 `memory.max` 被设置为当前用量加 512 MiB，随后在同一子树内突发申请 1024 MiB，从而形成可重复的 512 MiB 定向回收缺口；回收不能被无关的系统/根 cgroup 缓存吸收。压力按 64 MiB 连续申请且压力后不额外等待，避免 bin-only 依靠长时间后台回写追平。测试 slice 禁止 swap，以便主要观察文件页写回/回收而不是用匿名换出满足目标。<!-- lzx-note -->

同一内核、相同 seed 做两组消融：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-writeback-gate-config-lzx.json \
  --policy bin_lstm --scenario cold_writeback_gate_hot_reuse \
  --rounds 3 --seed 20260830

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-cold-writeback-gate-config-lzx.json \
  --policy bin_workload_lstm --scenario cold_writeback_gate_hot_reuse \
  --rounds 3 --seed 20260830
```

增强组除预测、绑定、驻留和 `FILE_DIRTY` 画像门禁外，还强制要求 `writepage_promotions>0`；否则该轮直接判为 `INVALID`，不能用普通深扫冒充“提前开放写回”。父 cgroup、压力 worker 等无应用身份节点允许最多 16 次 profile miss，内核在这种 miss 上必须直接回退原生路径；同时非预期 workload class 动作也限制为最多 16 次，防止安全回退预算掩盖策略污染。其他实验默认仍要求 profile miss 为 0。最终比较冷脏来源回收量、热干净页保留率、热页首次复用 refault/major fault/读取量/延迟，以及压力阶段 PSI、pgscan、写入量和 swap。收益判定必须同时满足“比 bin-only 少回收热干净页”和“新增写回/扫描成本没有抵消复用收益”。<!-- lzx-note -->
