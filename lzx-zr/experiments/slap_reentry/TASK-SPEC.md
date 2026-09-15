# 任务名称

实现并验证 **SLAP-style Segmented Reentry-Time Prediction → PARP Bin-Reclaim**。

---

# 1. 项目背景

当前项目为 PARP，目标是在 Linux 多应用内存压力场景下，根据用户未来应用访问行为调整 App/memcg 的回收优先级。

当前已经存在：

* 基于 App 使用历史的 LSTM；
* 输入包含最近 20 段应用历史及显式特征；
* 当前模型输出未来 30s / 180s 内 App 是否重新进入前台的概率；
* 内核侧已经存在 App priority → 8-bin → reclaim 的框架；
* 每个 App/memcg 内部仍使用原生 MGLRU；
* 修改范围：按用户后续修订，可按需修改 PARP/lzx/；独立目录仅用于组织实验。

本轮不重新设计内核，不修改 MGLRU。

---

# 2. 本轮实验的核心问题

当前 p30 / p180 模型主要回答：

```text
某个后台 App 是否会在未来 30s / 180s 内重新进入前台？
```

但实际内存回收需要回答：

```text
现在发生内存回收时，
当前后台 App 中，
哪些 App 距离下一次重新进入前台最远？
```

因此本轮将预测目标改为：

```text
remaining time to next foreground re-entry
```

并借鉴 SLAP 的思想，将连续重入时间转化为若干离散时间区间。

目标链路：

```text
App history / explicit features
        ↓
existing LSTM encoder
        ↓
segmented reentry-time prediction
        ↓
8-class probability
        ↓
coldness score
        ↓
App priority / 8-bin
        ↓
coldest bin first reclaim
        ↓
native MGLRU inside each memcg
```

---

# 3. 本轮实验原则

必须严格控制变量。

第一轮：

## 保持不变

* 数据源；
* train / validation / test 时间划分原则；
* 20-history 输入；
* duration 特征；
* opened_apps；
* 当前 foreground App；
* 当前停留时间；
* last-enter / last-leave gap；
* frequency 等显式特征；
* 原有 LSTM encoder；
* 内核 8-bin 框架；
* memcg 内 MGLRU；
* reclaim amount；
* 压力制造方式。

## 只修改

```text
预测标签
+
prediction head
+
App coldness score
```

## 暂时禁止加入

* Transformer；
* Survival model；
* Cox loss；
* ranking loss；
* page-level prediction；
* workload prediction；
* 动态 reclaim amount；
* 动态 bin 数；
* 恢复成本建模；
* MGLRU 修改。

---

# 4. 第一阶段：代码和数据审计

在开始修改前，先扫描：

```text
PARP/lzx-zr/
```

以及必要的参考代码（按用户后续修订，可按需修改）：

```text
PARP/lzx/
```

找到以下模块：

1. 当前 LSApp 数据预处理代码；
2. 当前 20-history 数据生成逻辑；
3. p30 / p180 label 生成代码；
4. 当前 LSTM 模型定义；
5. train / eval 脚本；
6. 当前 checkpoint；
7. 当前 app prediction 输出格式；
8. runtime prediction 入口；
9. App priority / bin 下沉接口；
10. 当前 8-bin reclaim 实现。

先生成：

```text
CODE-AUDIT.md
```

内容包括：

```text
当前文件
当前功能
本轮是否修改
预计修改方式
```

不要一上来重构项目。

---

# 5. 第二阶段：冻结当前 baseline

建立：

```text
PARP/lzx-zr/experiments/slap_reentry/
```

推荐结构：

```text
slap_reentry/
├── README.md
├── configs/
├── dataset/
├── models/
├── offline_eval/
├── runtime/
├── scripts/
├── tests/
└── outputs/
```

记录当前 baseline：

```text
M0 = 当前 20-history + explicit features + LSTM + p30/p180
```

保存：

* 当前训练参数；
* seed；
* checkpoint；
* test split；
* p30/p180 prediction；
* 当前评测结果；
* git commit；
* 关键文件 hash。

生成：

```text
BASELINE.md
```

必须保证后续实验能够和 M0 使用相同 test split。

---

# 6. 第三阶段：重新生成 Reentry-Time Dataset

## 6.1 核心定义

对于预测时刻 `t`，后台 App `a`：

```text
remaining_reentry_time(a, t)
    = next_foreground_entry_time(a) - t
```

重要：

不要计算：

```text
next_entry - last_exit
```

必须计算：

```text
next_entry - current_query_time
```

---

# 7. Query point 设计

生成两类 query point。

## A. Switch query

继续保留现有应用切换时刻。

## B. Periodic query

增加：

```text
period = 30 seconds
```

即：

```text
t
t + 30s
t + 60s
...
```

原因：

真实内存回收可以发生在任意时刻，而不一定紧随应用切换发生。

周期 query 必须只使用 `query_time` 之前的信息构造 feature，禁止任何 future leakage。

---

# 8. Candidate App 定义

对于每个 query time：

```text
candidate_apps
    = currently opened/resident apps
      - current foreground app
```

本轮在 LSApp 数据中沿用当前 `opened_apps` 逻辑作为 resident approximation。

必须在报告中注明：

```text
LSApp opened state != true PC resident process state
```

但本阶段不要修改这个定义。

---

# 9. Reentry segment 标签

第一版使用 8 个 segment：

```text
C0: [0, 30s)
C1: [30s, 60s)
C2: [60s, 180s)
C3: [180s, 300s)
C4: [300s, 600s)
C5: [600s, 1800s)
C6: [1800s, 3600s)
C7: [3600s, +∞)
```

即：

```text
0–30s
30–60s
1–3min
3–5min
5–10min
10–30min
30–60min
>60min
```

第一版不要优化边界。

这些时间段必须配置化，不要 hard-code 到模型内部。

例如：

```yaml
reentry_bins:
  - 30
  - 60
  - 180
  - 300
  - 600
  - 1800
  - 3600
```

---

# 10. Censoring 处理

必须正确处理 trace 结束导致的右删失。

例如：

```text
query_time = 10:50
trace_end = 11:00
App 未重新进入
```

只能知道：

```text
remaining_reentry > 10 min
```

不能直接赋成：

```text
C7
```

第一版本采用简单、安全策略：

```text
只有能够唯一确定 segment 的样本，
才参与 8-class cross entropy。
```

否则：

```text
label_valid = 0
```

并从 classification loss 中 mask。

不要人为把 censored sample 填成最大 segment。

输出数据至少包含：

```text
query_time
candidate_app
foreground_app
opened_apps

history_apps
history_durations

current_duration
last_enter_gap
last_leave_gap
frequency_features
time_features

next_entry_time
remaining_reentry_sec

reentry_class
is_censored
label_valid
```

---

# 11. 数据质量检查

实现 dataset validation script。

至少检查：

### 11.1 Future leakage

feature 中不得包含 query_time 之后的信息。

### 11.2 Monotonicity

同一 App 如果下一次 entry 没变化：

```text
t2 > t1
```

则：

```text
remaining(t2) < remaining(t1)
```

### 11.3 Segment correctness

手工抽样至少 100 个样本。

### 11.4 Train/test leakage

保持时间切分，禁止随机打散导致同一 session 跨 train/test。

### 11.5 Candidate validity

foreground App 不允许作为后台候选。

生成：

```text
DATASET-VALIDATION.md
```

如果 validation 不通过，不进入模型训练。

---

# 12. 第四阶段：实现 SLAP-style 模型 M1

不要重写现有 encoder。

复用：

```text
20-history
+
explicit features
+
current LSTM encoder
```

只替换 output head。

原来：

```text
LSTM
 ├─ p30
 └─ p180
```

新增：

```text
LSTM
  ↓
candidate representation
  ↓
Linear
  ↓
8 logits
  ↓
Softmax
```

模型命名：

```text
M1 = LSTM-Segmented-Reentry
```

Loss 第一版：

```text
CrossEntropyLoss
```

只在：

```text
label_valid == 1
```

时计算。

---

# 13. 不要只输出 argmax class

模型推理必须同时输出：

```text
p0 ... p7
```

以及：

```text
predicted_class = argmax(P)
```

另外计算：

```text
coldness_score
    = Σ(k * p_k), k=0..7
```

即：

$$
Score_a=\sum_{k=0}^{7}kP(y=k)
$$

范围约为：

```text
0 ~ 7
```

定义：

```text
score 越大
→ predicted reentry 越晚
→ App 越冷
→ reclaim priority 越高
```

保存完整 probability，不要只保存 class。

---

# 14. 第五阶段：Offline Evaluation

第一轮不接内核。

比较三个方法：

## B0 Recency

```text
score = time_since_last_foreground_usage
```

越久未访问越冷。

## B1 Current p180

使用当前模型：

```text
score = 1 - p180
```

越低的 180s 重入概率越冷。

## B2 SLAP-style

```text
score = Σ(k * p_k)
```

越大越冷。

必须使用：

```text
完全相同的 test queries
完全相同的 candidate set
```

---

# 15. 离线核心指标

不要把 8-class accuracy 当作主指标。

必须计算：

## 15.1 Classification metrics

辅助：

```text
Top-1 accuracy
Macro F1
per-class precision
per-class recall
confusion matrix
```

---

## 15.2 Pairwise Ordering Accuracy

对于同一 query 的两个可比较 App：

真实：

```text
remaining_A > remaining_B
```

如果：

```text
score_A > score_B
```

则 ordering correct。

定义：

```text
POA =
correct_pairs / comparable_pairs
```

这是本轮最重要的预测指标之一。

---

## 15.3 Cold Precision@K

对于每个 query：

真实按 remaining reentry time 从大到小排序。

模型按 score 从大到小排序。

计算：

```text
Cold Precision@1
Cold Precision@2
Cold Precision@3
```

---

## 15.4 Dangerous Victim Rate

模型选择最冷 App 作为 victim。

计算：

```text
DVR@30
    = P(real reentry < 30s | selected as victim)

DVR@180
    = P(real reentry < 180s | selected as victim)
```

越低越好。

---

## 15.5 Victim Reentry Time

对每个 query 的 top-1 victim：

统计真实：

```text
remaining_reentry_sec
```

输出：

```text
mean
median
P25
P75
P90
```

并画 CDF：

```text
X = victim actual reentry time
Y = CDF
```

越向右越好。

---

## 15.6 Multi-Victim Risk

对于预测最冷的前 K 个 App：

```text
K = 2
K = 4
```

计算：

```text
EVR_K =
min(real remaining reentry among selected victims)
```

越大越好。

---

# 16. 输出第一阶段报告

生成：

```text
OFFLINE-EVALUATION.md
```

必须包含对比表：

| Metric                  | Recency | p180 | SLAP-style |
| ----------------------- | ------: | ---: | ---------: |
| Pairwise ordering       |         |      |            |
| Cold Precision@1        |         |      |            |
| Cold Precision@2        |         |      |            |
| Cold Precision@3        |         |      |            |
| DVR@30 ↓                |         |      |            |
| DVR@180 ↓               |         |      |            |
| Victim median reentry ↑ |         |      |            |
| EVR@2 ↑                 |         |      |            |
| EVR@4 ↑                 |         |      |            |

同时保存：

```text
predictions.csv
ranking_results.csv
confusion_matrix
victim_reentry_cdf
segment_distribution
```

---

# 17. Gate-1

在 Offline Evaluation 完成前：

```text
禁止修改 kernel。
```

只有 SLAP-style 至少表现出明确 ranking improvement，才进入下一阶段。

参考 Gate，不作为绝对硬编码：

```text
Pairwise ordering:
相对 p180 提升约 >= 5%

并且下列指标至少两项明显改善：

Cold Precision@3 ↑
DVR@30 ↓
DVR@180 ↓
Victim median reentry ↑
EVR@2 / EVR@4 ↑
```

如果失败：

优先诊断：

```text
1. label 构造
2. query point 分布
3. class imbalance
4. feature 是否有效
5. opened_apps 映射问题
6. LSApp→PC domain gap
```

不要直接尝试 Transformer。

---

# 18. 第六阶段：Runtime 接入

仅 Gate-1 PASS 后执行。

runtime 流程：

```text
foreground/switch/periodic event
        ↓
construct current features
        ↓
M1 inference
        ↓
p0...p7
        ↓
coldness_score
        ↓
App priority
```

第一版本优先使用：

```text
predicted_class → existing bin
```

映射：

```text
C0 → Bin0
C1 → Bin1
...
C7 → Bin7
```

其中：

```text
Bin0 = hottest / reclaim last
Bin7 = coldest / reclaim first
```

同时保留：

```text
coldness_score
```

用于日志和后续 ranking 实验。

---

# 19. Runtime 日志

每次预测至少记录：

```text
timestamp
foreground_app
candidate_app

p0...p7
predicted_class
coldness_score

assigned_bin
domain_id / memcg mapping
sink_success
```

必须能追踪：

```text
prediction
→ app
→ bin
→ memcg
```

完整证据链。

---

# 20. 第七阶段：接现有 bin-reclaim

不要修改 memcg 内 MGLRU。

目标链路：

```text
SLAP predictor
    ↓
App bin
    ↓
coldest bin first
    ↓
memcg
    ↓
native MGLRU
```

保持现有：

```text
bin ordering
snapshot
RCU / priority structure
memcg lookup
reclaim path
```

只替换 App priority 来源。

---

# 21. 系统实验版本

至少比较：

```text
S0 Native
S1 PARP-p180
S2 PARP-SLAP
```

如果实现成本很低，再加入：

```text
S3 PARP-Recency
```

---

# 22. 系统实验必须控制

以下变量完全一致：

```text
application scenario
automation trace
RAM
swap
memory.high
memory.max
ballast
pressure level
reclaim amount
experiment duration
kernel version
MGLRU configuration
```

不要在比较 S1/S2 时修改其他 reclaim 参数。

---

# 23. 实验场景

至少实现：

## Scenario A：Fast Reentry

某 App 很快返回：

```text
A foreground
→ B
→ C
→ A
```

A 在约 30–180s 内返回。

验证是否避免误回收 future-hot App。

---

## Scenario B：Long Background

某 App 长时间后台驻留：

```text
A
→ B
→ C
→ D
```

A 很久不返回。

验证是否优先从长期不用 App 回收。

---

## Scenario C：Mixed Reentry

至少 4–6 个后台 App：

```text
App A: ~20s
App B: ~2min
App C: ~5min
App D: ~10min
App E: >20min
```

重点验证 App ordering。

---

# 24. 系统指标

## 24.1 Reclaim ownership

记录：

```text
reclaimed bytes per App
reclaimed bytes per bin
victim App
victim predicted class
victim actual next reentry time
```

必须先证明 SLAP 改变了 reclaim distribution。

---

## 24.2 Memory metrics

记录：

```text
workingset_refault
pgmajfault
pgscan
pgsteal
pswpin
pswpout
```

重点：

```text
refault
major fault
swap-in
```

---

## 24.3 User-visible metrics

在 App reentry 时记录：

```text
foreground restore latency
switch-to-usable latency
operation latency
P50
P95
P99
```

---

## 24.4 Overhead

记录：

```text
inference latency
CPU overhead
memory overhead
prediction frequency
snapshot update overhead
kernel lookup overhead
reclaim latency
PSI
```

---

# 25. 安全约束

沿用当前安全门。

不得为了制造结果：

```text
随意修改 memory.max
制造不可恢复 OOM
关闭已有安全保护
```

实验遇到异常压力必须停止 APPLY。

所有限制恢复必须可验证。

---

# 26. 最终输出目录

建议最终形成：

```text
outputs/slap-reentry-<timestamp>/
├── CONFIG.yaml
├── CODE-AUDIT.md
├── BASELINE.md
├── DATASET-VALIDATION.md
├── TRAINING-REPORT.md
├── OFFLINE-EVALUATION.md
├── RUNTIME-VALIDATION.md
├── KERNEL-EVALUATION.md
├── FINAL-REPORT.md
│
├── checkpoints/
├── predictions/
├── traces/
├── metrics/
├── figures/
└── logs/
```

---

# 27. 最终验收问题

最终必须明确回答以下问题，不允许只报告 classification accuracy。

## Q1

SLAP-style segmented reentry prediction 是否比当前 p180 更准确地排序后台 App？

看：

```text
Pairwise Ordering
Cold Precision@K
```

## Q2

它是否更少把即将返回的 App 选为 reclaim victim？

看：

```text
DVR@30
DVR@180
```

## Q3

它是否让 bin-reclaim 更多从真正长期不用的 App 回收？

看：

```text
per-App reclaimed bytes
victim actual reentry time
bin distribution
```

## Q4

这种更好的 App ordering 是否转化为真实系统收益？

看：

```text
refault
major fault
swap-in
reentry latency
P95/P99
```

## Q5

收益是否超过额外预测开销？

看：

```text
CPU
inference latency
memory
kernel overhead
PSI
```

---

# 28. 本轮成功标准

本轮不要以：

```text
8-class accuracy 很高
```

作为最终成功标准。

真正的成功链条应该是：

```text
Segmented Reentry Prediction
        ↓
better App ranking
        ↓
fewer dangerous victims
        ↓
better bin placement
        ↓
reclaim moves toward long-reentry Apps
        ↓
lower refault / major fault
        ↓
lower reentry latency
```

---

# 29. Codex 执行要求

按阶段执行。

每完成一个 Phase：

1. 运行对应测试；
2. 保存结果；
3. 更新报告；
4. 给出 PASS / FAIL；
5. 再进入下一阶段。

不要一次性大规模修改。

如果某阶段失败：

```text
先定位根因
→ 修复
→ 重跑
```

不要通过修改验收标准来绕过问题。

禁止在 Gate-1 之前修改 kernel reclaim 行为。

修改范围限制已由用户取消；可按需修改 PARP/lzx/，保留原有未提交修改。

---

# 最终目标

本轮实验不是证明 SLAP 本身，也不是追求复杂模型。

唯一目标是验证：

```text
将预测目标从
“未来 30/180s 是否进入”

改为

“未来多久重新进入前台的时间分段”

是否能够产生更好的 App reclaim ordering，
并进一步改善现有 PARP 8-bin reclaim 的真实内存表现。
```
