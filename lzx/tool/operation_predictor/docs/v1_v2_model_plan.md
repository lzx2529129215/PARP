# v1 / v2 模型版本说明

本文档说明当前模型版本划分和运行入口。

## 版本划分

### v1：Markov baseline

v1 保留当前 Markov 逻辑作为基线版本。

应用间预测：

- 模型文件：`v1/models/app_markov.py`
- 训练脚本：`v1/train/train_app_markov.py`
- 结果文件：`outputs/results/v1/app_markov_results.csv`

核心逻辑：

- 支持一阶到四阶 Markov。
- 按 horizon 分别维护转移表：`horizon -> context -> app`。
- 不统计 `history_apps` 内部转移。
- 只统计最后 `order` 个历史应用到未来窗口标签的关系。
- 退避顺序为：高阶 context -> 低阶 context -> horizon 全局未来标签频率。

应用内预测：

- 模型文件：`v1/models/op_markov.py`
- 训练脚本：`v1/train/train_op_markov.py`
- 结果文件：`outputs/results/v1/op_markov_results.csv`

应用内模型暂时保持 Markov，不做 LSTM。

### v2：应用间 LSTM，应用内暂不变

v2 只替换应用间预测模型。

应用间预测：

- 模型文件：`v2/models/app_lstm.py`
- 训练脚本：`v2/train/train_app_lstm.py`
- 结果文件：`outputs/results/v2/app_lstm_results.csv`
- checkpoint：`outputs/checkpoints/v2/app_lstm.pt`

应用内预测：

- 仍使用 Markov 逻辑，代码复制在 `v2/models/op_markov.py`
- 训练脚本为 `v2/train/train_op_markov.py`
- 不在 v2 中修改应用内逻辑

## v2 LSTM 输入输出

v2 LSTM 使用 `data/processed/train_app.pkl` 和 `data/processed/test_app.pkl`。

输入特征：

- `history_apps`：历史前台应用 ID 序列，输入 LSTM。
- `opened_apps`：当前已打开应用 multi-hot。
- `time_feature`：`[hour, weekday, is_weekend]`，训练时会归一化 hour 和 weekday。
- `user_group`：用户类型 ID，输入 embedding。

输出：

- `label_3min`
- `label_5min`
- `label_10min`

每个 horizon 使用一个独立的多标签输出头，输出维度等于应用数量。

损失函数：

- `BCEWithLogitsLoss`
- 三个 horizon 的 loss 取平均

评估指标：

- Hit@K
- Recall@K
- Precision@K
- MRR

## 依赖

v2 LSTM 依赖 PyTorch。

安装：

```bash
pip install -r requirements.txt
```

当前 `requirements.txt` 包含：

```text
torch
```

如果没有安装 PyTorch，`v2/train/train_app_lstm.py` 会直接报错并提示安装依赖，不会生成假结果。

## 运行 v1

运行当前 Markov 基线：

```bash
bash v1/scripts/run_pipeline.sh
```

该脚本会生成模拟数据、构造样本、切分数据集，然后训练：

- 应用间 Markov v1
- 应用内 Markov v1

主要输出：

```text
outputs/results/v1/app_markov_results.csv
outputs/results/v1/op_markov_results.csv
```

## 运行 v2

运行应用间 LSTM v2：

```bash
bash v2/scripts/run_pipeline.sh
```

该脚本会生成模拟数据、构造样本、切分数据集，然后训练：

- 应用间 LSTM v2
- 应用内 Markov v1

主要输出：

```text
outputs/results/v2/app_lstm_results.csv
outputs/checkpoints/v2/app_lstm.pt
outputs/results/v2/op_markov_results.csv
```

## 单独训练应用间 LSTM v2

如果样本已经存在，可以单独运行：

```bash
python v2/train/train_app_lstm.py \
  --train data/processed/train_app.pkl \
  --test data/processed/test_app.pkl \
  --epochs 20 \
  --batch-size 32 \
  --top-k 1 3 5 \
  --output outputs/results/v2/app_lstm_results.csv \
  --checkpoint outputs/checkpoints/v2/app_lstm.pt
```

## 单独训练应用间 Markov v1

```bash
python v1/train/train_app_markov.py \
  --train data/processed/train_app.pkl \
  --test data/processed/test_app.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v1/app_markov_results.csv
```
