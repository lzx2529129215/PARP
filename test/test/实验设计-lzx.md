# PARP 指标闭环实验设计

## 1. 范围与主版本

主基线固定为 Linux 6.17.13，唯一修改与编译源码树为 `lzx/kernel/src/linux-6.17.13-parp-lzx`。本轮只关注 PageFault、峰值异常、回收延迟和必要的安全/开销指标，专利不在范围内。

实验必须使用同一份可编译 effective-tier Apply 的内核，通过运行时开关完成消融。不能用两份不同来源的内核树分别代表 OFF 和 Apply。

## 2. LSAPP 与真实应用集合对齐

计分集合固定为九个无账号依赖应用：

| app_key | 模型词表 | 本地自动化内容 | 账号/网络 |
|---|---|---|---|
| FIREFOX | Firefox | 本地 HTML 翻页 | 不需要 |
| LIBREOFFICE | LibreOffice | 本地长文本翻页 | 不需要 |
| VLC | VLC | 本地 WAV 播放/暂停 | 不需要 |
| GIMP | GIMP | 本地 PPM 缩放 | 不需要 |
| AUDACITY | Audacity | 本地 WAV 播放/暂停 | 不需要 |
| THUNDERBIRD | Thunderbird | 本地 EML 阅读 | 不需要账号 |
| EVINCE | Evince | 本地 PDF 翻页 | 不需要 |
| FILES | Files | 本地仓库目录浏览 | 不需要 |
| CALCULATOR | Calculator | 本地按键输入 | 不需要 |

Telegram 虽然在 LSAPP 中样本多，但有登录依赖，不进入计分集合；WPS、QQ 保留在旧验收配置中，不混入这一版 LSTM 在线准确率实验。

统一契约由以下文件维护：

- `test/test/parp-lsapp-aligned-config-lzx.json`：自动化应用集合和内存比例；
- `lzx/tool/operation_predictor/data/vocab/lsapp_aligned/`：模型词表；
- `lzx/tool/operation_predictor/data/lsapp_aligned/mapping/lsapp_to_linux.json`：LSAPP 功能域映射；
- `lzx/service/configs/runtime/runtime_app_scope.lsapp_aligned.json`：窗口、进程、scope 和模型词表的运行时对应。

重新训练：

```bash
cd lzx/tool/operation_predictor
bash v3/scripts/run_lsapp_aligned_pipeline.sh
```

流水线使用时间顺序的 70/15/15 切分、next-switch 标签和 inverse-sqrt 类别权重。报告同时给出 micro Top-K、macro Top-K 和逐应用 Top-K，避免 Firefox 大类把总准确率“冲高”。

## 3. LSTM 在线验证门槛

先运行低压力 smoke，验证九个窗口都能启动、识别、切换和关闭；再运行 `full/hotcold`，每轮 90 次切换、10轮共900次。

Runtime Monitor 使用新 checkpoint、词表和 runtime scope 同时采集。结束后将 `online_lstm_predictions.csv` 与验收轮次的 `automation_trace.csv` 对齐：

```bash
python3 test/test/lstm-online-report-lzx.py \
  --automation-trace <round目录>/automation_trace.csv \
  --predictions <monitor目录>/online_lstm_predictions.csv \
  --runtime-scope lzx/service/configs/runtime/runtime_app_scope.lsapp_aligned.json \
  --output-dir <报告目录>
```

进入内存收益实验前至少满足：预测覆盖率不低于95%；Top-1、Top-3高于同集合随机切换基准；每个应用有有效样本；在线结果与 held-out 离线结果的差异需要单独解释。Top-K 只作为模型门槛，不能代替 PageFault 指标。

## 4. 两个机制的独立开关

| 变体 | PARP mode | effective-tier | Tier2全局/测试cgroup | 用途 |
|---|---:|---:|---:|---|
| native | 0 | 0 | 0/0 | 原生 MGLRU 基线 |
| effective | 0 | 3（bidirectional） | 0/0 | 只验证页面双向 effective-tier |
| tier2 | 0 | 0 | 1/1 | 只验证 Tier2 水位预测和主动回收 |
| tier2_bin | 2 | 0 | 1/1 | 验证 Tier2 + LSTM memcg-bin 排序 |
| combined | 2 | 3 | 1/1 | 验证双向 tier、Tier2 和 bin 的组合收益 |
| bin_off | 2 | 0 | 0/1（仅作用域标记） | LSTM 与 `/dev/myfs` 保持工作，原生随机 memcg-bin 基线 |
| bin_apply | 2 | 0 | 0/1（仅作用域标记） | 只打开 `vm.parp_reclaim_bin_enabled` 验证前台预测排序 |
| shadow_train | 0 | 1 | 0/0 | 完整候选 trace；只用于页面模型离线训练 |

`tier2` 不再隐含 memcg-bin；只有 `tier2_bin`/`combined` 同时打开 PARP
APPLY。这样主动回收和预测排序可以分别消融。测试工具在每轮前后读取开关，
发生漂移则该轮无效；运行结束恢复原状态。<!-- lzx-note -->

`bin_off` 与 `bin_apply` 的 LSTM 服务、预测批次、PARP APPLY、场景计划和
cgroup 作用域完全相同，唯一处理变量是 `vm.parp_reclaim_bin_enabled=0/1`。
两者均保持 Tier2 全局开关、effective-tier 和 WSS 为 0，因此不会把主动回收
或页面 tier 保护的效果计入 bin-reclaim。<!-- lzx-note -->

Apply 能力需要编译，但运行时默认仍为 OFF。使用目标源码树内的统一构建入口：

```bash
LZX_EXPERIMENTAL_APPLY=1 \
  lzx/kernel/src/linux-6.17.13-parp-lzx/tools/parp/build_lzx_kernel.sh \
  all <基础config> <构建目录>
```

其中 `CONFIG_PARP_TIER2`、`CONFIG_PARP_EFFECTIVE_TIER` 和
`CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY` 集中在 LZX PARP Kconfig
菜单。正式配对使用 `all` 生成的一份内核，只改变运行时 treatment。

<!-- lzx-note -->

## 5. 单机制首轮实验

每个机制先只做一轮 pilot，不据此宣布达标：

```bash
# 生成 Native 基线及可重放计划
python3 test/test/parp-acceptance-lzx.py run \
  --config test/test/parp-lsapp-aligned-config-lzx.json \
  --profile full --suite hotcold --rounds 1 --seed 20260812 --variant native

# effective-tier 只改变一个开关，并重放 Native 的逐步计划
python3 test/test/parp-acceptance-lzx.py run \
  --config test/test/parp-lsapp-aligned-config-lzx.json \
  --profile full --suite hotcold --rounds 1 --variant effective \
  --replay-from <上一步输出目录>

# Tier2 独立 pilot
python3 test/test/parp-acceptance-lzx.py run \
  --config test/test/parp-lsapp-aligned-config-lzx.json \
  --profile full --suite hotcold --rounds 1 --variant tier2 \
  --replay-from <Native输出目录>
```

pilot 主要检查方向和副作用：PageFault、major fault、真实 refault、direct reclaim 次数/P95、kswapd CPU、测试 slice CPU/IO。方向合理且无安全回退后，再运行冷热10轮和峰值3轮。

LSTM 因果验证使用 `parp-predictive-reclaim-config-lzx.json`：held-out LSAPP
链先产生“当前应用→下一应用”预测，再启动有界 headroom 压力，最后切换并
访问真实下一应用。`--sequence-mode random_negative` 保持相同压力器但故意
破坏标签，用作预测价值的负对照。<!-- lzx-note -->

页面模型不得继续把工程 fixture 当训练结果。先用 `shadow_train` 收集至少
跨多个 session 的完整候选与未来 5 秒真实访问，再执行：

```bash
python3 test/test/build-effective-tier-dataset-lzx.py \
  --round-dir <shadow输出>/round-01 \
  --round-dir <shadow输出>/round-02 \
  --output-dir test/outputs/effective-tier-training
```

入口会拒绝候选采样、trace loss、重复 session identity 或非 SHADOW 数据；
它只生成离线 `ranking_model.json`，不会自动把未经审核的模型写入内核。
<!-- lzx-note -->

## 6. Native/OFF 与 Apply 精确配对

基线每轮保存独立的 `scenario-plan.json`，包含应用序列、是否访问冷区、字节偏移、访问量、停留时间、内存布局和 OOM burst 设置。Apply 使用 `--replay-from` 逐项重放，而不只是重复一个 seed。

完成两侧后生成配对结果：

```bash
python3 test/test/paired-report-lzx.py \
  --baseline <Native目录>/summary.json \
  --optimized <Apply目录>/summary.json \
  --output-dir <配对报告目录>
```

报告只有在以下条件全部满足时才计算正式改善率：逐轮计划 SHA-256 相同；同一主机、内存、swap、VM sysctl、CPU governor；同一 kernel release 和 config；两侧轮次全部有效；开关状态没有漂移。建议按 AB/BA 交替执行，降低温度、缓存和时间顺序偏差。

## 7. 非零 OOM 基线校准

峰值场景增加一个仅位于测试 slice 的匿名内存 burst。burst 设置 `oom_score_adj=1000`，发生 cgroup OOM 时优先牺牲探针，宿主 OOM 仍立即判整轮无效。默认每20个计分步骤重复一次，覆盖“已有峰值压力时继续启动/切换应用”。

从 Native 开始按 `0.45、0.50、0.55、0.60、0.65` 的物理内存比例逐级校准：

```bash
python3 test/test/parp-acceptance-lzx.py run \
  --config test/test/parp-lsapp-aligned-config-lzx.json \
  --profile full --suite peak --rounds 1 --variant native \
  --oom-probe-ratio 0.50
```

选择最小且满足以下条件的比例并冻结：三轮 `failure_total` 合计大于0；宿主 `oom_kill=0`；至少90%的计分步骤完成；不存在 MemAvailable/PSI 硬中止；不出现“每次 burst 必然 OOM”的饱和状态。随后 Apply 必须从该 Native 目录重放，不能重新调压。

OOM结果同时报告 `memory.events:oom`、`oom_kill`、应用启动/自动化失败和低内存窗口。若 Native 仍为0，结论保持 `NOT_EVALUABLE_ZERO_BASELINE`，不得用 PageFault 改善代替峰值指标。
