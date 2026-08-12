# v4.1-PARP：应用间 LSTM 影响验证层

v4.1-PARP 基于 [`../v4-parp`](../v4-parp)，目标是验证“应用间 LSTM 的 next-App 预测是否真的改善 PARP 的 App 级内存决策”。它不把 PyTorch 放进内核回收热路径，也不打开 PARP Apply。

## 共用的内核源码

v4.1 不再维护另一套 Linux 源码或构建目录。内核修改直接位于
[`../v4-parp/src/linux-6.17.13-parp-lzx`](../v4-parp/src/linux-6.17.13-parp-lzx)，
并且始终复用 `../v4-parp/build/effective-tier-live-shadow-r9-lzx`。
`kernel/patches/v4.1-snapshot-observability.patch` 作为可移植补丁保留，其
`snapshot`/`stats` 功能已合入共用源码。

增量编译统一使用：

```bash
../v4-parp/scripts/build-kernel-lzx.sh
```

## 验证闭环

```text
operation_predictor/v2 AppLSTM
        │
        ├─ normalized app predictions
        ▼
v4.1 App prior adapter ──> app_bind/app_prior audit commands
        │
        ├─ Native baseline：只保护当前前台 App
        └─ LSTM counterfactual：加入 next-App headroom 与 App 回收预算
                │
                ▼
        hit@k / MRR / 启动 headroom 误差 / 下一 App 回收预算变化
```

v4.1 的评估是用户态、只读、反事实计算：不会写 debugfs，不安装内核，不切换 PARP 模式，不改变 MGLRU、generation、扫描预算、swap 或预取行为。

## 快速运行可复现 fixture

```bash
cd /home/lzxxxxxx/桌面/huawei/myself-kswapd/lzx/kernel/v4.1-parp
python3 tools/make_fixture.py
python3 tools/evaluate_app_lstm_effect.py \
  --samples samples/fixture/samples.csv \
  --app-states samples/fixture/app_states.csv \
  --predictions samples/fixture/lstm_predictions.csv \
  --output-dir outputs/fixture
```

结果位于 `outputs/fixture/summary.json` 和 `outputs/fixture/per_sample.csv`。

生成某个样本对应的 v4-parp 控制面审计命令：

```bash
python3 tools/emit_parp_commands.py \
  --sample-id s001 \
  --app-states samples/fixture/app_states.csv \
  --predictions samples/fixture/lstm_predictions.csv \
  --output outputs/fixture/s001_parp_commands.sh
```

生成的 shell 文件只用于审计和人工复核，默认不执行。

## 使用真实 App-LSTM

现有 v2 LSTM 推理需要 PyTorch。输入格式见 `samples/fixture/lstm_input.csv`：

```bash
python3 tools/run_app_lstm.py \
  --input samples/fixture/lstm_input.csv \
  --checkpoint ../../tool/operation_predictor/outputs/checkpoints/app_lstm/lsapp_app_lstm.pt \
  --output outputs/fixture/lstm_predictions_real.csv \
  --score-mode softmax \
  --model-version 401
```

然后把 `lstm_predictions_real.csv` 替换评估命令中的预测文件。`sigmoid` 输出在 v4.1 中只作为未校准分数，进入 headroom 计算前会在候选集合内归一化，不会直接冒充概率。

## 目录

- `tools/v41_core.py`：无依赖的 v4.1 参考策略和指标计算。
- `tools/run_app_lstm.py`：复用 `operation_predictor/v2` 的 AppLSTM 推理适配器。
- `tools/emit_parp_commands.py`：将 LSTM 结果转成 v4-parp `app_bind/app_prior` 审计命令。
- `tools/evaluate_app_lstm_effect.py`：Native 与 LSTM 反事实对照。
- `tools/make_fixture.py`：确定性合成 fixture 生成器。
- `configs/default.json`：horizon、TTL、保护权重和安全开关。
- `tests/`：纯 Python 单元测试。

## 解释结果时的边界

命中率只能说明预测质量；真正的“作用”需要同时观察 headroom 误差和下一 App 的回收预算变化。v4.1 目前只验证 App 层先验是否值得传入 PARP，不声称已经证明页面级 reclaim 收益。真实内核收益仍需在匹配 commit/config/release 的 PARP observe 内核上采集，并保留 Native 对照。
