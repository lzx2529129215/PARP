# PARP R1–R5 应用内真实操作场景实现说明

日期：2026-08-31  
状态：代码、配置、自动化 dry-run 和单元测试完成；尚未执行 Native/PARP 正式配对轮次。<!-- lzx-note -->

## 一、实现结果

在保留原 M1–M5 受控机制实验的基础上，新增了 R1–R5 真实交互套件。R 场景的应用工作集和压力后复用均由真实 GUI 操作产生，不再通过 fixture socket 模拟应用页面访问。

每轮 `scenario.json` 明确记录：

```text
synthetic_app_working_set = false
working_set_kind = application_native_ui
pressure_kind = memory_reclaim_of_application_native_working_sets
```

五个场景生成的命令中不存在：

- `memory-fixture-lzx.py`
- `reclaim-substitution-fixture-lzx.py`
- `oom_threshold_pressure_lzx.py`
- `PREPARE`、`MADV_COLD`、`REDIRTY`
- `TOUCH_FILE`、`TOUCH_ANON`、`TOUCH_CLEAN`

## 二、真实应用资产

离线资产生成器升级为 schema v2，每轮使用相同内容且不依赖网络或账号：

| 应用 | 内容与操作 |
|---|---|
| Firefox/Epiphany | 带 8 张 4096×4096 图片、长文本和 4096×4096 Canvas 的本地 HTML；滚动和搜索 |
| Thunderbird | 大型本地 EML；滚动邮件正文 |
| VLC | 90 秒本地 WAV；播放、暂停和跳转 |
| GIMP | 连续打开多张 4096×4096 图片；缩放、画笔拖动 |
| LibreOffice | 原生可编辑 ODT；翻页、追加 4096 段文本并保存 |
| Evince | 240 页本地 PDF；连续翻页 |
| Image Viewer | 打开高分辨率图片并缩放 |
| Solitaire | 新开牌局并执行键盘操作 |

共享资产缓存中的 ODT 不会被应用直接修改：runner 为每轮复制独立 ODT；其余只读资产允许硬链接，保证不同内核使用相同内容。

## 三、五个自动化场景

### R1：`r1_app_cold_retire`

1. 启动 8 个 GUI 应用。
2. 每个应用执行上述真实操作，建立应用自身的 anon/file/cache 工作集。
3. 五个预测冷应用各使用一次。
4. 执行训练序列 `Thunderbird -> Firefox -> Thunderbird -> Firefox -> VLC`。
5. LSTM 和 `/dev/myfs` 门禁通过后，对这些真实应用页面请求固定回收量。
6. 不再返回五个冷应用，统计逐应用 cgroup 回收来源。

### R2：`r2_app_predicted_return`

前五步与 R1 相同。压力后切回 Firefox，自动执行：首页定位、14 次连续翻页、搜索 `Project section 1200`、确认结果。复用窗口采集 Firefox 和父 cgroup 的 fault/refault、swap-in、I/O、PSI 及动作耗时。

### R3：`r3_app_source_distribution`

压力后依次真实复用 Firefox、Thunderbird 和 VLC。该场景同时统计五个冷应用和三个热应用的 `memory.current`、anon/file 下降，回答固定回收量来自哪些真实应用。

### R4：`r4_app_dirty_substitution`

应用工作集建立后：

1. GIMP 选择画笔并在画布上执行 6 次拖动。
2. LibreOffice 在 ODT 尾部追加 4096 段内容并保存。
3. 重放五步训练序列，消除编辑动作对 LSTM 历史的影响。
4. 压力前读取各真实应用 scope 的 `memory.stat`。
5. 只有冷应用合计 `file_dirty` 达到配置门槛才允许进入回收。
6. 压力后复用三个热应用，比较 Native、bin-only 和增强策略。

这里不再用合成 dirty 页面补足容量。应用提前 fsync/writeback 导致脏页不足时，本轮为 `INVALID`。

### R5：`r5_app_writeback_gate`

R5 使用与 R4 相同的真实编辑和复用动作，并额外：

1. 每轮临时设置 `vm.laptop_mode=600`。
2. 压力前再次取证 `laptop_mode`。
3. cold-aggressive 组要求 `writepage_promotions>0`。
4. 任意退出路径恢复原 sysctl。

因此 R5 不能用普通深扫描冒充“原先禁止 writepage、增强策略提前开放”的收益。

## 四、压力触发方式

R1–R5 不再运行匿名分配器。所有待回收内存都先由真实应用操作建立，然后向实验父 cgroup 的 `memory.reclaim` 请求配置的 256 MiB 回收量。

有效性要求：

- 8 个应用工作集总量至少 768 MiB；
- 实际回收量至少达到目标的 50%；
- `memory.events` 中没有新增 OOM/OOM kill；
- 8 个应用 scope 在压力前后 device/inode 一致；
- Native/PARP 使用同一资产、应用顺序、操作和 seed。

每轮还会写出路径无关的 `action-plan.json`。它锁定资产 SHA-256、应用角色、训练历史、回收目标和全部 GUI 操作；Apply 使用 `--replay-from <Native session>` 校验每轮计划哈希，任何差异都会在启动实验前标记为 `BLOCKED`。

该方式消除了“匿名压力 worker 自身页面混入应用结果”的问题，但 `memory.reclaim` 仍属于受控实验触发器，而不是自然整机压力。真实结果应表述为“应用原生工作集上的受控回收”，不能表述成无人工边界的长时间桌面负载。

## 五、输出指标

每轮至少输出：

- 逐应用 `memory.current`、anon、file、file_dirty；
- 冷/热应用回收来源占比；
- `workingset_refault_file`、`workingset_refault_anon`；
- `pgfault`、`pgmajfault`；
- `pswpin`、`pswpout`；
- `pgscan_direct`、`pgsteal_direct`；
- 逐应用和父 cgroup 的 PSI `some/full total`；
- 块层读写量；
- `REAL_REUSE_*` 用户操作耗时；
- reclaim-bin、cold-aggressive 和 workload profile 内核动作。

R4/R5 不再具有 fixture 的逐 inode `mincore()` clean/dirty 精确归因，因此 R 场景用于验证真实体验，M4/M5 继续作为页面机制证据。

## 六、运行方式

R1–R3 先做 Native 与 bin-only 配对：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-real-interaction-config-lzx.json \
  --policy native_kernel --scenario r2_app_predicted_return \
  --rounds 3 --seed 20260831

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-real-interaction-config-lzx.json \
  --policy bin_lstm --scenario r2_app_predicted_return \
  --rounds 3 --seed 20260831 \
  --replay-from <上一步Native输出目录>
```

R5 做同一 r11 内核运行时消融：

```bash
python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-real-interaction-config-lzx.json \
  --policy bin_lstm --scenario r5_app_writeback_gate \
  --rounds 3 --seed 20260831

python3 test/test/parp-real-pc-experiment-lzx.py run \
  --config test/test/parp-real-interaction-config-lzx.json \
  --policy bin_workload_lstm --scenario r5_app_writeback_gate \
  --rounds 3 --seed 20260831
```

## 七、实现与验证位置

- 配置：[parp-real-interaction-config-lzx.json](/home/lzx/Desktop/PARP/test/test/parp-real-interaction-config-lzx.json)
- runner：[parp-real-pc-experiment-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-real-pc-experiment-lzx.py)
- 离线资产：[create_real_pc_assets_lzx.py](/home/lzx/Desktop/PARP/test/automation/create_real_pc_assets_lzx.py)
- 应用启动规格：[parp-acceptance-lzx.py](/home/lzx/Desktop/PARP/test/test/parp-acceptance-lzx.py)
- 场景测试：[test_real_interaction_scenarios.py](/home/lzx/Desktop/PARP/test/test/tests/test_real_interaction_scenarios.py)

已完成验证：

- Python 静态编译通过；
- 5 个 R 场景均通过完整 automation dry-run；
- R 场景专项测试 6 项通过；
- test 独立测试 41 项通过；
- 验收测试 23 项通过；
- runtime service 回归测试 181 项通过，3 项按环境跳过；
- schema-v2 资产实生成成功，共 15 个离线文件，ODT 可正常作为 ZIP/ODF 读取。
