# v4.1-PARP 验证记录

## 环境

- Python 3.10.12
- CPU-only PyTorch 2.3.1
- checkpoint：`operation_predictor/outputs/checkpoints/app_lstm/lsapp_app_lstm.pt`
- model version：401
- 评估 horizon：5 分钟（300000 ms）
- PARP Apply：关闭

## 运行内容

```bash
cd /home/lzx/Desktop/huawei_mem/lzx/MGLRU-test/v4.1-parp
python3 tools/make_fixture.py
python3 tools/run_app_lstm.py \
  --input samples/fixture/lstm_input.csv \
  --checkpoint ../../operation_predictor/outputs/checkpoints/app_lstm/lsapp_app_lstm.pt \
  --output outputs/fixture/lstm_predictions_real.csv \
  --score-mode softmax --model-version 401 --device cpu
python3 tools/evaluate_app_lstm_effect.py \
  --samples samples/fixture/samples.csv \
  --app-states samples/fixture/app_states.csv \
  --predictions outputs/fixture/lstm_predictions_real.csv \
  --output-dir outputs/fixture/real_lstm
```

## Fixture 结果

| 指标 | 结果 |
|---|---:|
| 样本数 | 6 |
| 预测覆盖率 | 1.0000 |
| hit@1 | 0.5000 |
| hit@3 | 0.8333 |
| MRR | 0.6667 |
| 平均真实启动页数 | 3500.0 |
| 平均 LSTM 预计启动页数 | 2505.6 |
| 平均 headroom 绝对误差 | 2886.0 |
| Native 平均总回收目标 | 983.3 页 |
| LSTM 平均总回收目标 | 2989.5 页 |
| 下一 App 平均预算变化（LSTM 相对 Native 的减少量） | -429.2 页 |

负的预算减少量表示：该样本中，LSTM 为未运行候选 App 增加了启动 headroom，导致总回收目标增加；这不是实现错误，而是需要在真实数据中进一步权衡的策略结果。

以上结果只证明“现有 LSTM 输出可以进入 v4.1 的 App 级反事实链路”。fixture 只有 6 个合成样本，不能作为真实内存收益或线上效果结论。真实验证还需要 runtime_monitor/automation 产生的带时间对齐数据，以及匹配的 PARP observe 内核。
