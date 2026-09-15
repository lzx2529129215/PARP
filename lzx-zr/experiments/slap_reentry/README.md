# SLAP-style segmented reentry → PARP bin reclaim

当前执行用户指定的第一版分段实验；不声称复现某一未提供全文的SLAP论文。

本轮目录：`outputs/slap-reentry-20260911-v1`。阶段状态见 `progress.json`，训练日志见 `logs/training.log`。原始M0和精确时间划分均冻结在 `baseline/`。

用户已取消 lzx-zr-only 与 lzx-read-only 限制。为保留可审查的对照，本轮目前仍使用独立目录，旧模块没有改动。Gate-1前不改变内核行为；只有排序明确改善才进入runtime。

从 PARP 根目录运行（新实验需复制config并改变output，禁止覆盖已有数据）：

```bash
python3 -m pip install --target lzx-zr/experiments/slap_reentry/.deps -r lzx-zr/experiments/slap_reentry/requirements-plot.txt
export PYTHONPATH="/home/lzx/Desktop/PARP/lzx-zr/experiments/slap_reentry/.deps${PYTHONPATH:+:$PYTHONPATH}"
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/dataset/build.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/dataset/validate.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s lzx-zr/experiments/slap_reentry/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/scripts/train.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/offline_eval/evaluate.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/scripts/expand_rankings.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/offline_eval/diagnose.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/scripts/verify_checkpoint.py --config lzx-zr/experiments/slap_reentry/configs/first.json
PYTHONDONTWRITEBYTECODE=1 python3 lzx-zr/experiments/slap_reentry/scripts/report.py --config lzx-zr/experiments/slap_reentry/configs/first.json
```

绘图依赖独立安装到`.deps/`；模型训练使用现有PyTorch环境。若推理和metrics文件已完成、仅绘图/报告中断，运行`offline_eval/evaluate.py --config <config> --reports-only`恢复；不必重做训练/推理。`expand_rankings.py`为排序CSV补充每个query的完整有序候选、分数和实际返回/删失信息，可重复执行。

`scripts/report.py`包含本轮结果的文字解读；后续实验须按新结果审阅更新结论，不能复制本轮诊断。`verify_checkpoint.py`只做离线输入重放和CPU推理计时，不写内核；主机后台负载未隔离，计时不等于在线系统开销。

- 分段 `[0,30), [30,60), [60,180), [180,300), [300,600), [600,1800), [1800,3600), [3600,∞)`，配置化。
- 当前时刻之后的首次进入，剩余时间=`next_entry-query_time`，不使用上次离开作为起点。
- 观察不足且无法唯一确定类别：valid=0；未返回但完整观察≥3600秒可以判C7，仍保留censored标记，不能伪造具体重入时间。
- 新增30秒查询按既有时间边界分区；不重算边界。原M0事件+180秒query逐条匹配，保留original_index，B0/B1/B2始终相同test查询与候选。
- 两种评测范围均报告：全部新增30秒query、原M0query子集；各自区分单候选和多候选。
- 原session可跨全局时间边界，标签在边界截断；不是随机拆分，也不声称session-disjoint。
- opened沿用原片段开始状态；LSApp opened state != true PC resident process state。
- B0使用可观察的完整会话中最后foreground退出距query的间隔；缺失记unknown，排序值保守置0。M1显式特征仍只看20段，二者定义分别披露。
- 保留LSTM、embedding、duration/opened编码、shared层、显式32维分支；末层输出每App 2→8，使用masked CE，无class weighting、ranking、survival loss。
- 冷度=`Σ k p_k`是期望分段编号，不是期望秒数。
- C7没有有限上界；不填假时间，不将所有删失当∞。
- CP@K主指标只用真实Top-K集合可唯一确定、候选数>K、真值边界无并列（允许删失候选数量≤K的可证明集合）的query，排除选全候选得到1的平凡情况。
- POA只统计可证明先后的pair，包括一个有确切返回、另一个删失下界足够长的pair；模型分数并列算未正确排序。
- DVR排除观察不足的未知结果；victim CDF是已观察返回的条件分布，须同时查看删失比例。
- EVR@K只统计候选数≥K且最早返回可确定的query。
- Gate：multi-candidate POA相对p180提升≥5%，用户成组paired bootstrap CI>0；至少两项其他指标在common-known查询上明确改善。详见config及评估报告。
- 已有内核patch中低编号物理bin先回收；C0热/C7冷的逻辑类编号不能不经验证直接写进内核。Gate通过后审查映射，保留原MGLRU。

报告：`CODE-AUDIT.md`、`BASELINE.md`、`DATASET-VALIDATION.md`、`TRAINING-REPORT.md`、`OFFLINE-EVALUATION.md`、`FINAL-REPORT.md`。后续runtime/kernel报告在Gate未通过时标NOT RUN，不把离线改善当系统收益。
