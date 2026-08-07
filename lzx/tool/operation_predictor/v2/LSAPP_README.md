# LSAPP 接入 v2 LSTM 说明

本文档只说明 LSAPP 数据集如何接入 v2 的应用间 LSTM。v1 不使用 LSAPP，v2 的应用内 `op` Markov 也不从 LSAPP 构造。

## 运行入口

在项目根目录执行：

```bash
bash v2/scripts/run_lsapp_pipeline.sh
```

如果只想先做 1 个 epoch 的冒烟训练：

```bash
EPOCHS=1 BATCH_SIZE=2048 bash v2/scripts/run_lsapp_pipeline.sh
```

脚本会优先使用 `.venv-wsl/bin/python`。如果环境里没有 PyTorch，脚本会直接退出并提示安装依赖。

## 输入文件

LSAPP 训练链路依赖这些输入：

```text
data/raw/datasets/LSApp/after_mapping/add_opened_apps/lsapp_mapped_with_opened.clean.tsv
data/raw/datasets/LSApp/tool/lsapp_to_app_vocab.csv
data/vocab/app_vocab.json
data/vocab/user_group_vocab.json
```

`lsapp_mapped_with_opened.clean.tsv` 是当前实际进入训练前的数据源。它去掉了 NUL 等会影响 TSV 读取的脏字符。文件里虽然带有旧的 `opened_apps` 列，但 v2 准备脚本不再信任这一列作为模型输入。

`lsapp_to_app_vocab.csv` 是 LSAPP 应用名到项目应用词表的权威映射文件。当前准备脚本读取的是已经映射后的 clean TSV，但这个映射文件仍是 LSAPP 映射链路的来源，应继续维护在 `data/raw/datasets/LSApp/tool/` 下。

`app_vocab.json` 是模型最终可预测的应用集合。`foreground_app` 和实时重建出来的 `opened_apps` 都会过滤到这个词表内。

`user_group_vocab.json` 用来把 `user_group` 转成模型输入 id。LSAPP 当前没有细分用户群，默认统一写成 `通用用户`。

## 总体流程

`v2/scripts/run_lsapp_pipeline.sh` 按顺序执行这些主要阶段：

```text
clean TSV
  -> data/raw/lsapp/app_events.csv
  -> data/processed/lsapp/app_samples.pkl
  -> data/processed/lsapp/train_app.pkl / val_app.pkl / test_app.pkl
  -> outputs/results/v2/lsapp_app_lstm_val_results.csv
  -> outputs/checkpoints/v2/lsapp_app_lstm.pt
```

这些输出全部放在 `data/.../lsapp/` 专属目录，或使用 `lsapp_` 文件名前缀，不覆盖模拟数据产物。

## 产物 1：app_events.csv

路径：

```text
data/raw/lsapp/app_events.csv
```

生成命令：

```bash
python scripts/tools/prepare_lsapp_app_events.py \
  --input data/raw/datasets/LSApp/after_mapping/add_opened_apps/lsapp_mapped_with_opened.clean.tsv \
  --output data/raw/lsapp/app_events.csv \
  --app-vocab data/vocab/app_vocab.json
```

生成逻辑来自：

```text
scripts/tools/prepare_lsapp_app_events.py
```

它会把 LSAPP clean TSV 转成项目统一的应用事件 CSV，字段固定为：

```text
user_id,timestamp,foreground_app,opened_apps,user_group
```

字段含义：

- `user_id`：LSAPP 用户 id。如果原始表头第一列被 tar 头污染，脚本会把这个被污染的第一列仍识别为 `user_id`。
- `timestamp`：当前事件发生时间。
- `foreground_app`：当前前台应用，也就是这一行用户正在打开或交互的应用。
- `opened_apps`：按事件流实时重建出的当前已打开应用集合，用分号连接。
- `user_group`：用户群组，LSAPP 当前统一写 `通用用户`。

关键清洗规则：

- 只保留 `Opened` 和 `User Interaction` 两类事件。
- `opened_apps` 不再从 clean TSV 里已有的 JSON 列读取，因为该列来自旧的 `session_opened_apps.tsv`，本质上是整段 session 应用序列，存在未来泄漏。
- 脚本会按 `(user_id, session_id)` 维护一个实时 opened set：`Opened` 加入应用，`Closed` 移除应用，`User Interaction` 保证当前前台应用在 opened set 中。
- 输出行只保留 `Opened` 和 `User Interaction`，但 `Closed` 事件仍会参与更新 opened set。
- 应用名会先做规范化，`大宗点评` 统一修正为 `大众点评`。
- `foreground_app` 不在 `app_vocab.json` 中的行会被跳过。
- `opened_apps` 中不在 `app_vocab.json` 中的应用会被丢弃。
- 如果当前前台应用不在当前行 `opened_apps` 中，脚本会把它补进去，保证当前状态自洽。

这个文件的用途是作为后续样本构造的标准事件表。它还是人最容易检查的数据层，因为 CSV 可以直接用文本工具或表格工具查看。

## 产物 2：app_samples.pkl

路径：

```text
data/processed/lsapp/app_samples.pkl
```

生成命令：

```bash
python src/data/build_app_dataset.py \
  --input data/raw/lsapp/app_events.csv \
  --app-vocab data/vocab/app_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/lsapp/app_samples.pkl \
  --history-len 5 \
  --horizons 3 5 10
```

生成逻辑来自：

```text
src/data/build_app_dataset.py
```

它会按 `user_id` 分组，并按时间排序。对每个用户的每个可用时刻构造一个训练样本：

- `history_apps`：当前时刻之前最近 5 个前台应用 id。
- `opened_apps`：当前时刻已打开应用的 multi-hot 向量。
- `time_feature`：当前时刻的时间特征，包括小时、星期几、是否周末。
- `user_group`：用户群组 id。
- `label_3min`：当前时刻之后 3 分钟内出现过的应用 multi-hot 标签。
- `label_5min`：当前时刻之后 5 分钟内出现过的应用 multi-hot 标签。
- `label_10min`：当前时刻之后 10 分钟内出现过的应用 multi-hot 标签。

这里的标签是 horizon-specific 的，也就是 3、5、10 分钟分别有自己的未来应用集合。构造标签时只会向后扫描到最大 horizon，也就是 10 分钟，避免把更远的未来应用算进当前训练目标。

这个文件的用途是保存完整 LSAPP 应用间预测样本。它是模型训练前的核心数据集，但 `.pkl` 不适合人工直接阅读，主要给 Python 训练脚本读取。

## 产物 3：train_app.pkl / val_app.pkl / test_app.pkl

路径：

```text
data/processed/lsapp/train_app.pkl
data/processed/lsapp/val_app.pkl
data/processed/lsapp/test_app.pkl
```

生成命令：

```bash
python src/data/split_dataset.py \
  --input data/processed/lsapp/app_samples.pkl \
  --task app \
  --output-dir data/processed/lsapp
```

生成逻辑来自：

```text
src/data/split_dataset.py
```

它会先按样本的 `timestamp` 做全局时间排序，再按默认比例切分：

```text
train: 70%
val:   15%
test:  15%
```

这三个文件的用途：

- `train_app.pkl`：训练 LSTM 参数。
- `val_app.pkl`：训练结束后做验证，用于调参和观察模型表现。
- `test_app.pkl`：最终测试集，训练脚本不会读取它；只在单独评估脚本中使用。

使用时间顺序切分的原因是应用预测本质上是时间序列任务。这样可以减少随机切分带来的未来信息混入训练集问题。

## 产物 4：lsapp_app_lstm_val_results.csv

路径：

```text
outputs/results/v2/lsapp_app_lstm_val_results.csv
```

生成命令位于流水线第 4 步：

```bash
python v2/train/train_app_lstm.py \
  --train data/processed/lsapp/train_app.pkl \
  --val data/processed/lsapp/val_app.pkl \
  --epochs 20 \
  --batch-size 32 \
  --top-k 1 3 5 \
  --output outputs/results/v2/lsapp_app_lstm_val_results.csv \
  --checkpoint outputs/checkpoints/v2/lsapp_app_lstm.pt
```

这里不会读取 `test_app.pkl`。如果要对最终 test set 评估已经生成的 checkpoint，使用：

```bash
python v2/eval/eval_app_lstm.py \
  --checkpoint outputs/checkpoints/v2/lsapp_app_lstm.pt \
  --test data/processed/lsapp/test_app.pkl \
  --output outputs/results/v2/lsapp_app_lstm_eval_results.csv
```

结果 CSV 字段：

```text
version,model,horizon,k,hit_at_k,recall_at_k,precision_at_k,mrr,num_samples,train_loss
```

字段含义：

- `version`：模型版本，这里是 `v2`。
- `model`：模型名称，这里是 `app_lstm`。
- `horizon`：预测窗口，当前为 3、5、10 分钟。
- `k`：Top-K 的 K 值，当前为 1、3、5。
- `hit_at_k`：Top-K 中是否命中任意真实未来应用。
- `recall_at_k`：真实未来应用集合被 Top-K 覆盖的比例。
- `precision_at_k`：Top-K 预测中正确应用的比例。
- `mrr`：首个命中应用的倒数排名。
- `num_samples`：参与验证的样本数。
- `train_loss`：最后一个 epoch 的训练 loss。

这个文件的用途是对比模型效果。后续调模型结构、训练轮数、batch size 或 LSAPP 映射规则时，主要看这个 CSV 的指标变化。

## 产物 5：lsapp_app_lstm.pt

路径：

```text
outputs/checkpoints/v2/lsapp_app_lstm.pt
```

生成逻辑同样来自：

```text
v2/train/train_app_lstm.py
```

它保存的是训练后的 LSTM checkpoint，里面包含：

- `model_state_dict`：模型参数。
- `args`：训练参数。
- `num_apps`：应用词表大小。
- `num_user_groups`：用户群组数量。
- `horizons`：模型训练使用的预测窗口。

这个文件的用途是复用训练好的模型。后续如果要做单条样本推理、继续训练或部署测试，应从这个 checkpoint 加载参数。

## 当前已生成规模

最近一次完整 LSAPP 准备和 1 epoch 冒烟训练生成的规模：

```text
data/raw/lsapp/app_events.csv:              1,987,090 rows
data/processed/lsapp/app_samples.pkl:       1,871,811 samples
data/processed/lsapp/train_app.pkl:         1,310,267 samples
data/processed/lsapp/val_app.pkl:             280,771 samples
data/processed/lsapp/test_app.pkl:            280,773 samples
outputs/results/v2/lsapp_app_lstm_val_results.csv: 9 rows
```

其中结果 CSV 是 3 个 horizon 和 3 个 Top-K 的组合：

```text
3min:  Top-1 / Top-3 / Top-5
5min:  Top-1 / Top-3 / Top-5
10min: Top-1 / Top-3 / Top-5
```

## 检查命令

检查 LSAPP 标准事件表是否还含有错别字：

```bash
rg "大宗点评" data/raw/lsapp
```

检查 PyTorch 环境：

```bash
.venv-wsl/bin/python -c "import torch; print(torch.__version__)"
```

查看结果：

```bash
head outputs/results/v2/lsapp_app_lstm_val_results.csv
```
