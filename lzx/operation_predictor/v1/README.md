# v1 Markov Baseline

v1 保留当前 Markov baseline。

包含：

- `models/app_markov.py`：应用间 Markov，支持 1 到 4 阶，按 horizon 维护转移表。
- `models/op_markov.py`：应用内 Markov，支持 1 到 4 阶。
- `train/train_app_markov.py`：训练并评估应用间 Markov。
- `train/train_op_markov.py`：训练并评估应用内 Markov。
- `scripts/run_pipeline.sh`：v1 完整流水线入口。

运行：

```bash
bash v1/scripts/run_pipeline.sh
```

输出：

```text
outputs/results/v1/app_markov_results.csv
outputs/results/v1/op_markov_results.csv
```
