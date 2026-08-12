# PARP / MGLRU 验收实验

本目录实现当前 r9 Shadow 内核的诊断基线，以及后续同源 Native/OFF 与 Apply 内核的成对验收。当前 r9 的 `apply_compiled=0`，因此首轮结果只能标记为 `DIAGNOSTIC_BASELINE`，不能宣称 PageFault 或峰值异常已经降低。

当前基线数值、验收目标换算、结果限制和复现实验说明见 [`baseline-results-lzx.md`](baseline-results-lzx.md)。

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
- 当前 `schema-v3` 已把 zhj 中适合正式验收的严格检查合并到本主线：cgroup 首尾路径与 device/inode 必须一致，`memory.stat`、`memory.events`、`cpu.stat`、`io.stat` 和 `memory.current` 必须可读，必需计数器不得缺失或倒退；任一条件失败都使该轮无效，原始文件仍保留。
- trace 除 ring 丢失外，还检查 direct reclaim 与 memcg reclaim 的 begin/end 嵌套、孤立 end、未闭合 begin 和关键事件解析失败；配对错误不会被静默丢弃，也不会用成功配对数掩盖。
- CPU/I/O 使用同一个测试 slice 的 `cpu.stat` 与 `io.stat` 首尾差分，报告 CPU 总时间、单核等价占比、整机占比、块层读写量和吞吐；页缓存命中的读取不计入块层 I/O。
- 每个应用报告“启动动作开始到匹配 X11 窗口验证成功”的就绪代理延迟，并输出轮内均值与 P95。该值不是首个可交互帧；峰值场景先并发启动再逐个验证，因此应视为启动就绪上界。
- 每次实验根目录写出 `system-metadata-lzx.json`，记录内核 release/config 哈希/命令行、CPU、内存、swap、VM sysctl、THP、CPU governor、X11 会话和结果文件系统，用于检查 OFF/Apply 环境是否同源。
- OOM必须拆分为测试cgroup `oom`、测试cgroup `oom_kill` 和宿主 `oom_kill`；前两项用于说明测试边界，宿主OOM会使该轮立即无效。
- trace ring 固定为每 CPU 1 MiB 并持续流式读取；任一 `overrun/commit overrun/dropped events` 非零都使该轮无效，避免大 ring 自身污染内存压力。
- 峰值异常总数为自动化动作/启动失败、低内存窗口命中和测试 cgroup `oom_kill` 的合计。宿主 `oom_kill` 不计入成绩，而是立即中止并判该轮无效。

随机不等于不可复现：每轮先由 seed 生成序列并保存 `scenario.json`，优化前后必须复用完全相同的 seed 和场景。正式改善率为 `(基线均值 - 优化均值) / 基线均值 * 100%`。

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

工具只在 `parp-acceptance.slice` 设置有限的 `MemoryHigh` 和 `MemoryMax`，不修改 PARP/MGLRU 模式、swappiness、水位、swap 配置，不调用 `drop_caches` 或 `memory.reclaim`。宿主 `oom_kill` 增加会立即停止；`MemAvailable < 2 GiB` 连续 3 个采样也会停止。PSI full avg10 大于 0.20 会持续记录，只有它与 `MemAvailable < 4 GiB` 同时连续出现 3 次才作为硬中止，避免把仍有大量可用内存的正常应用冷启动误判为危险。测试 cgroup 内的 OOM 会被记录，但不会放宽宿主保护线。

为获得测试 slice 的 `cpu.stat`/`io.stat`，runner 会用已授权 sudo 对当前用户的 `user-UID.slice` 和 `user@UID.service` 设置运行时 `CPUAccounting=yes`/`IOAccounting=yes`。这只启用当前启动周期的 cgroup controller 计数，不写持久配置，重启后由下一次实验自动重新启用。

预检还要求至少保留 1024 个 inotify watch。大型源码树的 IDE 文件监视器可能耗尽 watch，导致 systemd 无法观察测试 scope 退出并卡在清理阶段；这种情况下工具会在正式运行前阻止采集。

稀疏文件 sidecar 在退出时自动删除；报告、trace 和自动化日志保留在 `lzx/tool/outputs/parp_acceptance/`。

## 使用方法

先做预检和单轮小规模冒烟：

```bash
cd /home/lzxxxxxx/桌面/huawei/myself-kswapd
python3 test/test-parp-acceptance-lzx.py -v
python3 test/parp-acceptance-lzx.py preflight --profile smoke --suite all
python3 test/parp-acceptance-lzx.py run --profile smoke --suite hotcold
python3 test/parp-acceptance-lzx.py run --profile smoke --suite peak
```

冒烟通过后执行当前内核完整诊断基线：

```bash
python3 test/parp-acceptance-lzx.py run --profile full --suite hotcold --seed 20260812
python3 test/parp-acceptance-lzx.py run --profile full --suite peak --seed 20260812
```

每次运行首先打印 `output=...`。持续日志为该路径内的 `round-NN/automation.log`、`round-NN/monitor.csv` 和 `round-NN/trace/stream-error.txt`；汇总为 `summary.md` 与 `summary.json`。`round-NN/round-result.json` 中的 `validity.invalid_reasons` 会列出 cgroup 端点、trace 配对、监控样本或启动就绪的具体失效原因；`launch`、`cgroup`和 `system` 分别保存启动延迟、CPU/I/O/回收和宿主差分指标。

## 扩展应用建议

首版不依赖额外安装。正式扩展可加入 Blender、QEMU/KVM + virt-manager、Ollama，分别覆盖图形渲染、虚拟机和本地大模型峰值。新应用必须先补齐启动命令、窗口识别、无外部副作用的 UI 操作、scope 归属和失败检测，未通过预检时只能记为 `NOT_INSTALLED/SKIP`，不能算作成功步骤。
