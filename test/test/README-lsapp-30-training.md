# LSApp 30 应用离线重训

使用审阅过的 30 应用映射，保留旧 15 个实际应用的词表 ID；PAD=30、UNKNOWN=31 不进入候选名单。训练输出 32×2 概率，实际应用为 30 个。

从 PARP 根目录执行，输出目录必须使用新名字：

```bash
python3 test/test/prepare_lsapp_30.py --output-dir test/outputs/lsapp-30-data-NEW
python3 test/test/run_lsapp_30_ablation.py \
  --dataset test/outputs/lsapp-30-data-NEW/dataset \
  --output-dir test/outputs/lsapp-30-training-NEW --epochs 20 --threads 2
python3 test/test/audit_lsapp_30_candidates.py \
  --dataset test/outputs/lsapp-30-data-NEW/dataset \
  --output test/outputs/lsapp-30-training-NEW/candidate-coverage.json
python3 test/test/evaluate_lsapp_30_recall.py \
  --training-dir test/outputs/lsapp-30-training-NEW \
  --vocab lzx/tool/operation_predictor/data/vocab/lsapp_30/app_vocab_duration.json
python3 test/test/verify_lsapp_30_checkpoint.py \
  --training-dir test/outputs/lsapp-30-training-NEW
```

六组为 5/10/20 段历史，分别不加/加入显式特征。每组按验证损失选择 epoch，按验证集后台 30 秒 AP 选择最终模型；测试集只报告结果。高召回阈值也仅从验证集选择，不部署到线上。

`progress.json` 显示数据准备阶段或训练组别、epoch；`training_history.json` 逐轮更新；`summary.json` / `REPORT.md` 随每组完成更新。全部完成后生成 `selected_checkpoint.pt`、`RETRAINING-REPORT.md` 和 `high-recall-evaluation.json`。

检查点复验从原始片段开始时间重建测试集前 128 个输入，与批量评估概率核对，输出 `checkpoint-verification.json` 和 `prediction-example.json`。示例使用词表 ID，不能直接当作内核运行时 ID 写入。

当前运行：`test/outputs/lsapp-30-training-20260910-v1`，日志在同级 `lsapp-30-training-20260910-v1.log`。

```bash
tail -f test/outputs/lsapp-30-training-20260910-v1.log
cat test/outputs/lsapp-30-training-20260910-v1/progress.json
```

数据保持真实时间轴、事件＋180 秒采样、3600 秒间隔会话、600 秒时长归一化上限、时间顺序 70/15/15 划分和分区内观察掩码。UNKNOWN 片段保留，防止删除未映射事件后把两次进入错误合并。原始应用关闭先更新原始身份集合，再映射，避免多个来源映射到同一目标时误删候选。

“进入”沿用连续前台身份改变的定义；连续同身份 Opened/Interaction 会合并，因此不等同于每一个原始 Opened 事件。打开集合沿用 builder 的片段开始状态。LSApp 打开/关闭也不证明 PC 进程驻留。候选内召回率与所有曾访问应用返回的覆盖率分别报告，后者是包含关闭重启的代理指标。

显式特征 checkpoint 的结构版本为 `app_visit_window_explicit_v2_offline`，需使用对应模型和相同历史/特征构建器加载，不能直接塞进旧单步在线入口。本轮不修改常驻服务或内核接口。

验证：

```bash
python3 -m unittest discover -s test/test/tests -p 'test_lsapp_30*.py'
python3 -m unittest discover -s test/test/tests -p 'test_visit_explicit.py'
```
