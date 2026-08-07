# v2 应用间 LSTM 版本操作说明

v2 的版本边界：

- 应用间预测：从 v1 Markov 改为 LSTM。
- 应用内预测：暂时不改，仍使用 Markov。

## 目录结构

```text
v2/
├── models/
│   ├── app_lstm.py          # 应用间 LSTM 模型
│   └── op_markov.py         # 应用内 Markov，逻辑暂时沿用 v1
├── train/
│   ├── train_app_lstm.py    # 训练并在 val 上验证应用间 LSTM
│   └── train_op_markov.py   # 训练并评估应用内 Markov
├── scripts/
│   └── run_pipeline.sh      # v2 一键流水线
└── README.md
```

公共的数据生成、样本构造、切分和指标工具仍复用：

```text
scripts/data/make_synthetic_data.py
src/data/build_app_dataset.py
src/data/build_op_dataset.py
src/data/split_dataset.py
src/eval/metrics.py
src/utils/io_utils.py
```

## 环境

当前已经配置了 WSL 本地虚拟环境：

```text
.venv-wsl/
```

`v2/scripts/run_pipeline.sh` 会优先使用：

```bash
.venv-wsl/bin/python
```

当前已验证的环境：

```text
torch 2.12.0+cpu
numpy 2.4.4
cuda False
```

说明：当前是 CPU 版 PyTorch，训练可以跑，只是没有 CUDA 加速。

如果需要重新安装依赖：

```bash
python3 -m venv .venv-wsl
.venv-wsl/bin/python -m pip install --upgrade pip
.venv-wsl/bin/python -m pip install torch numpy --index-url https://download.pytorch.org/whl/cpu
```

Windows PowerShell 环境也已经安装过 `torch` 和 `numpy`，但建议运行 bash 流水线时使用 `.venv-wsl`，避免 Windows Python 和 WSL Python 混用。

## 一键运行

在项目根目录执行：

```bash
bash v2/scripts/run_pipeline.sh
```

该脚本会依次执行：

1. 生成模拟数据。
2. 构造应用间样本。
3. 构造应用内样本。
4. 切分应用间 train/val/test。
5. 切分应用内 train/val/test。
6. 训练应用间 LSTM v2，并只在 val 上验证。
7. 训练并评估应用内 Markov。

## LSAPP 数据集

LSAPP 接入 v2 LSTM 的说明已经单独整理到：

```text
v2/LSAPP_README.md
```

里面详细说明了运行命令、原始输入、每个中间产物如何生成，以及这些产物在训练流程中的作用。

## 输入文件

词表：

```text
data/vocab/app_vocab.json
data/vocab/op_vocab.json
data/vocab/user_group_vocab.json
```

流水线中间产物：

```text
data/raw/app_events.csv
data/raw/op_events.csv
data/processed/app_samples.pkl
data/processed/op_samples.pkl
data/processed/train_app.pkl
data/processed/val_app.pkl
data/processed/test_app.pkl
data/processed/train_op.pkl
data/processed/test_op.pkl
```

## 输出文件

应用间 LSTM v2 结果：

```text
outputs/results/v2/app_lstm_val_results.csv
outputs/checkpoints/v2/app_lstm.pt
```

应用内 Markov 结果：

```text
outputs/results/v2/op_markov_results.csv
```

## 单独训练应用间 LSTM

如果数据已经构造并切分好，可以只训练 LSTM：

```bash
.venv-wsl/bin/python v2/train/train_app_lstm.py \
  --train data/processed/train_app.pkl \
  --val data/processed/val_app.pkl \
  --epochs 20 \
  --batch-size 32 \
  --top-k 1 3 5 \
  --output outputs/results/v2/app_lstm_val_results.csv \
  --checkpoint outputs/checkpoints/v2/app_lstm.pt
```

训练脚本不会读取 `test_app.pkl`。最终测试集评估请使用 `v2/eval/eval_app_lstm.py`，避免训练阶段接触 test。

常用参数：

- `--epochs`：训练轮数，默认 20。
- `--batch-size`：batch size，默认 32。
- `--lr`：学习率，默认 `1e-3`。
- `--hidden-dim`：LSTM hidden size，默认 64。
- `--app-embedding-dim`：应用 embedding 维度，默认 32。
- `--opened-dim`：`opened_apps` 编码维度，默认 32。
- `--top-k`：评估 Top-K，例如 `1 3 5`。
- `--device`：`auto`、`cpu` 或 `cuda`。当前环境建议保持 `auto` 或 `cpu`。

## 单条推理应用间 LSTM

如果已经有 checkpoint，可以用推理脚本输出 3/5/10 分钟的 Top-K 应用和概率：

```bash
python v2/infer/infer_app_lstm.py \
  --checkpoint outputs/checkpoints/v2/lsapp_app_lstm.pt \
  --history-apps 飞书 华为浏览器 腾讯QQ 华为浏览器 夸克浏览器 \
  --opened-apps 飞书 夸克浏览器 \
  --timestamp "2018-01-16 06:26:26" \
  --user-group 通用用户 \
  --top-k 5 \
  --score-mode softmax
```

输出字段：

```text
horizon,rank,app_id,app,probability,score_mode
```

`--score-mode softmax` 会让同一个 horizon 下所有应用概率归一化求和为 1。模型训练时使用的是多标签 BCE，因此如果想看每个应用独立的命中分数，也可以使用：

```bash
--score-mode sigmoid
```

对应的推理执行脚本位于：

```bash
D:\lzx\school\lzx_code\huawei_mem\lzx\operation_predictor\scripts\lzx\v2\app\lstm_infer.sh
```

## 单独训练应用内 Markov

```bash
.venv-wsl/bin/python v2/train/train_op_markov.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v2/op_markov_results.csv
```

## 查看结果

查看 LSTM 结果前几行：

```bash
head outputs/results/v2/app_lstm_val_results.csv
```

查看应用内 Markov 结果前几行：

```bash
head outputs/results/v2/op_markov_results.csv
```

当前 LSTM 结果 CSV 字段：

```text
version,model,horizon,k,hit_at_k,recall_at_k,precision_at_k,mrr,num_samples,train_loss
```

含义：

- `horizon`：预测窗口，当前为 3、5、10 分钟。
- `k`：Top-K。
- `hit_at_k`：Top-K 是否命中任一真实应用。
- `recall_at_k`：真实标签覆盖率。
- `precision_at_k`：Top-K 中正确项占比。
- `mrr`：首个命中项的倒数排名。
- `train_loss`：最后一个 epoch 的训练 loss。

## 已验证结果

当前环境已经成功运行：

```bash
bash v2/scripts/run_pipeline.sh
```

训练日志最后显示：

```text
epoch 20/20 train_loss=0.188858
saved results: outputs/results/v2/app_lstm_val_results.csv
saved checkpoint: outputs/checkpoints/v2/app_lstm.pt
saved: outputs/results/v2/op_markov_results.csv
```

## 常见问题

### 1. 提示 PyTorch 未安装

先确认脚本是否使用 `.venv-wsl/bin/python`：

```bash
.venv-wsl/bin/python -c "import torch; print(torch.__version__)"
```

如果失败，重新安装：

```bash
.venv-wsl/bin/python -m pip install torch numpy --index-url https://download.pytorch.org/whl/cpu
```

### 2. 为什么没有 CUDA

当前安装的是 CPU 版 PyTorch，`torch.cuda.is_available()` 为 `False`。目前数据量很小，CPU 足够跑通 v2。

### 3. 能不能直接用 PowerShell 的 python

可以单独运行 Python 脚本，但 bash 流水线默认使用 WSL。为了路径和环境一致，建议优先使用：

```bash
bash v2/scripts/run_pipeline.sh
```
