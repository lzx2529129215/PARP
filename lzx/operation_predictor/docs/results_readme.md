# Results 文件阅读指南

目的：说明如何快速查看并解读 `outputs/results` 目录下模型评估与样本预览文件。

目录文件说明：

- `v1/app_markov_results.csv`：v1 应用间 Markov 评估结果。每行对应一组超参（如马尔可夫阶数、horizon、k）的聚合指标。
- `v2/app_lstm_results.csv`：v2 应用间 LSTM 评估结果。每行对应一个 horizon 和 top-k 的聚合指标。
- `v1/op_markov_results.csv` / `v2/op_markov_results.csv`：应用内 Markov 评估结果，v2 阶段暂时沿用该应用内模型逻辑。
- `app_samples_preview.csv` / `op_samples_preview.csv`：若干样本的输入/真实标签/模型预测 top-k 示例，用于人工检查样例质量与输出格式。
- `dataset_preview.html`：数据集的可视化预览，可在浏览器中打开查看分布和示例。

常见列与含义：

- `model`：模型标识（例如 `app_markov`）。
- `order`：马尔可夫模型的阶数（或模型相关的序列长度参数）。
- `horizon`：预测的时间或步长窗口（模型上看未来多远）。
- `k`：top-k 值（预测候选数）。
- `hit_at_k`：命中率（top-k 是否包含任一真实标签的比例，越大越好）。
- `recall_at_k`：召回率（覆盖真实标签的比例，适用于多标签场景）。
- `precision_at_k`：精确度（top-k 中正确预测的比例）。
- `mrr`：平均倒数排名（Mean Reciprocal Rank），强调首个命中的排名位置。
- `num_samples`：用于计算该行指标的样本数（样本数小则指标方差大）。

如何快速查看：

1. PowerShell（查看前几行）：

```powershell
Get-Content lzx\operation_predictor\outputs\results\v1\app_markov_results.csv -TotalCount 12
```

2. Python（用 pandas 加载并查看统计）：

```python
import pandas as pd
df = pd.read_csv("lzx/operation_predictor/outputs/results/v1/app_markov_results.csv")
print(df.head())
print(df.describe())
```

3. 在浏览器中打开数据预览：

```powershell
start lzx\operation_predictor\outputs\results\dataset_preview.html
```

示例行解读（来自 `v1/app_markov_results.csv`）：

```
v1,app_markov,4,10,5,0.3269,0.1811,0.1189,0.1946,52
```

- 含义：使用 `app_markov`（4 阶马尔可夫）、`horizon=10`、`k=5` 时，`hit@5≈0.327`（大约 32.7% 的样本在 top-5 有命中），`MRR≈0.195`，评估样本数为 52。

解读建议：

- 对比不同 `k` 或 `horizon` 的行可以看模型在不同召回粒度与预测窗口下的表现趋势。 
- 若 `num_samples` 很小，避免过度解读单行结果，建议合并更多测试样本或查看置信区间。
- 使用 `*_samples_preview.csv` 检查单条样本预测是否合理（特别是当指标看起来异常时）。

如需，我可以：
- 把该 README 加入仓库导航或在脚本中自动生成更详细的报告；
- 或把 `*_samples_preview.csv` 的前 N 行渲染为更易读的表格并保存为 HTML。
