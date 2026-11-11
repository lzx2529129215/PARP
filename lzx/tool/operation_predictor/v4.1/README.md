# RankProp v4.1 应用重入预测

当前 PC 常驻服务的时序预测权重。Transformer 根据最多 32 次前台切换、发生时刻、切换间隔及候选应用的近期性与频率，预测未来 1–16 次切换内的返回距离和窗口内未返回类别。有界候选集残差进一步修正排序分数。

## 文件与调用

- `model/rankprop_v4_1.ts` 为当前服务使用的 TorchScript 权重。
- `train.py` 包含网络结构、输入编码和训练目标。仓库根目录通过脚本位置定位。
- `predict.py` 是按时间顺序读取 JSON Lines 的离线推理入口。
- `outputs/training.json` 保存训练参数、验证集选择记录和输入数据摘要。
- 服务实际调用 [OnlineRankProp41Predictor.predict_bundle](../../../service/runtime_monitor/predictor_rankprop41.py)。

在仓库根目录运行，依赖 Python、PyTorch 和 NumPy。

```bash
python3 lzx/tool/operation_predictor/v4.1/predict.py < events.jsonl
```

输入示例

```json
{"app":"Firefox","timestamp":"2026-11-11 10:00:00"}
{"app":"LibreOffice","timestamp":"2026-11-11 10:00:10"}
{"app":"GIMP","timestamp":"2026-11-11 10:00:20"}
```

应用名称以 `data/vocab/lsapp_expanded/app_vocab_duration.json` 为准。输入 `{"reset":true}` 开始新会话。

`predict.py` 输出的 `eviction_order` 从较高回收优先级开始。`eviction_score` 越大表示预计越晚返回。服务 API 使用真实存活候选，并按保护顺序返回候选行，同时提供条件下一应用概率、期望返回距离及回收排序字段。

训练脚本使用 `data/lsapp_expanded/processed/app_lstm_duration_switch/` 下的 train、val、test 分区，其末尾的历史模型对照还使用 `test/outputs/rankprop_v3_compare_20261030/decisions.csv`。准备对应数据后可按原训练参数复现。

当前服务的复用感知融合见 [v4.2](../v4.2/README.md)。
