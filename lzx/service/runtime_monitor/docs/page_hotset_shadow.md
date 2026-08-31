# 文件热页聚类与下一状态预测（Shadow）

`PageHotsetShadow` 是 Runtime Monitor 内的纯观测组件。它消费现有 eBPF
`page_access` 事件，在用户态构建每个前台应用的文件页工作集，并预测下一次
工作集状态变化。它不会执行预取、回收、page-cache 删除、cgroup/debugfs 写入或
其他内核策略操作。

## 启动

文件热页功能没有 procfs 近似回退，必须启用 eBPF 文件事件源：

```bash
cd /home/lzx/Desktop/PARP/lzx/service
python3 runtime_monitor/monitor.py \
  --file-event-source ebpf \
  --require-ebpf-file-events \
  --foreground-backend desktop \
  --direct-x11-events \
  --enable-page-hotset-shadow
```

默认参数：

- 固定窗口 1000 ms，封窗延迟 500 ms；
- 300 个有效非空前台窗口后首次训练；
- 每新增 60 个有效窗口后台重训；
- 每个应用最多保留最近 3600 个有效窗口；
- 窗口覆盖率不低于 80% 的页面为基础热页；
- 桶内覆盖率不低于 50% 的页面为桶热页。

对应参数为 `--page-hotset-window-ms`、`--page-hotset-lateness-ms`、
`--page-hotset-warmup-windows`、`--page-hotset-retrain-windows`、
`--page-hotset-history-windows`、`--page-hotset-base-coverage` 和
`--page-hotset-bucket-coverage`。

## 页面、窗口与质量边界

页面身份为：

```text
device_major + device_minor + inode + page_index
```

`page_index` 使用运行内核的基础页大小。输出只保存该身份和连续 page range，
不保存文件原始路径。

事件按事件自身的 wall-clock 时间戳进入窗口，而不是按用户态 drain 时间归档。
只有整个窗口内前台应用不变、文件事件源可用且没有投递/perf/归属缺口时，窗口
才有资格训练。空窗口会写入审计文件，但不会训练，并会切断 Markov 上下文。
后台应用的页事件不会进入前台模型。

## 模型

每个应用单独训练。基础热页从快照中剥离后，非空残差集合以二进制 Jaccard
距离运行有界、确定性的 k-medoids；分别评估 K=2、3、4 和 5，只有轮廓系数
比当前候选高超过 0.02 时才选择更多桶，因此差异在 0.02 以内时选择更小、更稳定的
K。每桶至少需要 `max(10, 5%×残差窗口数)` 个成员。

残差为空时使用独立的 `BASE_ONLY` 状态。在线快照低于所属桶训练相似度第 5
百分位（且最低阈值为 0.1）时输出 `UNKNOWN`，不会强制塞入某个桶。

状态序列会压缩连续相同桶。预测首先查询二阶 `(前一桶, 当前桶)` 转移，未见
上下文时回退一阶 `当前桶` 转移。`UNKNOWN`、无效/空窗口及前台 epoch 边界会
切断序列。模型在独立 spawn 进程中训练，完成后原子替换用户态模型；旧版本的
未决预测以 `MODEL_REPLACED` 结束。

## 输出与验收

每个 session 新增：

```text
model/page_snapshots.jsonl
model/page_hotset_models/<app>/<version>.json
prediction/page_hotset_predictions.jsonl
prediction/page_hotset_outcomes.jsonl
review/page_hotset_summary.md
```

预测页集合为“基础热页 ∪ 下一桶热页”。实际下一不同桶首次被观察后，结果文件
记录桶 Top-1、页面 recall/precision/Jaccard、预测页放大率和提前时间。每个应用
至少有 30 条因果有效结果后才给出 PASS/FAIL：默认要求页面 recall ≥ 80%，且
预测页数/实际页数 ≤ 2。汇总同时报告基础热页、保持当前桶和全局热门桶基线。

首版只覆盖 page-cache 读取路径，不覆盖 mmap 后的普通内存访问和匿名页；未被
预测的页只能称为“未预测页”，不能据此断言为冷页。
