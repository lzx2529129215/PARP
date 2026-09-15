# 30 秒 / 180 秒应用进入概率

这条链路使用 LSApp-expanded 的 15 个应用，预测未来窗口内发生进入事件的概率。持续前台使用不算新的进入；A→B→A 中返回 A 算进入。窗口为 `(t,t+30s]`、`(t,t+180s]`，同一应用在窗口内进入多次仍是一个正标签。

模型、数据和在线入口与旧 v3 单步模型分开保存。当前常驻服务仍使用旧配置；新入口只输出预测，不写 `/dev/myfs` 或 debugfs，也不启用应用回收控制器。

## 输入与概率结构

输入包括最近 5 段应用及其时长、有效性掩码、已打开应用集合、当前前台应用、小时/星期/周末特征和用户组。当前段只使用已过去的时间。与原数据处理一致，段时长最小为 1 秒；归一化上限为 600 秒。输入不足 5 段时左侧 padding，模型内部先将有效 token 移到左侧再打包，保证短历史不会丢失。

LSTM 编码器为每个应用输出 `z1,z2`：

```text
p_visit_30s  = sigmoid(z1)
p_visit_180s = p_visit_30s + (1 - p_visit_30s) * sigmoid(z2)
```

`sigmoid(z2)` 表示前 30 秒未进入时，在接下来 150 秒内进入的条件概率。两窗口最终概率联合接受二元交叉熵监督，并天然满足 `0 <= p30 <= p180 <= 1`。不同应用间不归一化。

概率来源标为 `nested_sigmoid_uncalibrated`：模型未单独做概率校准；阈值的实际效果需要参考评估结果。

## 构建与训练

在仓库根目录执行：

```bash
bash lzx/tool/operation_predictor/v3/scripts/run_lsapp_visit_window_pipeline.sh
```

默认复用 `data/lsapp_expanded/raw/app_events.csv` 和原词表，生成 `data/lsapp_expanded/processed/app_visit_window_v1`，训练产物位于预测工程的 `outputs/lsapp_expanded/visit_window_v1`。

数据保留事件＋180 秒周期采样、3600 秒会话间隔、时间顺序 70%/15%/15% 划分。时间戳相同的样本不会分到两个分区。新标签不再排除当前应用，并保留没有未来进入的负样本。每个应用、每个窗口分别存储 `valid_visit_30s` / `valid_visit_180s` 掩码：观察不足且未进入时不能标负；已发生进入的标签仍有效。标签不读取分区边界之后的事件。

默认训练 20 轮、batch size 2048、Adam 学习率 0.001、seed 42、CPU 线程 2，按验证集损失保存最佳 checkpoint。BCE 不沿用旧单标签分类的类别权重。现有产物不会被覆盖。再次训练可指定新的输出目录：

```bash
OUTPUT_DIR=outputs/lsapp_expanded/visit_window_run2 \
  bash lzx/tool/operation_predictor/v3/scripts/run_lsapp_visit_window_pipeline.sh
```

`evaluation.json` 给出独立验证/测试结果，`*_predictions.npz` 保存预测、标签、有效掩码及后台候选标志，`*_rows.jsonl` 保存逐行样本身份。PR-AUC 使用 average precision 的阶梯积分，同分数作为同一阈值；没有正样本时返回 null。空热/冷名单的准确率返回 null，不算达标。

## 单次预测

输入 JSON 使用词表中的名称，例如 `Firefox`、`LibreOffice`、`Files`：

```json
{
  "history_apps": ["Firefox", "LibreOffice", "Files", "Firefox", "LibreOffice"],
  "history_durations_s": [120, 60, 15, 90, 20],
  "history_mask": [1, 1, 1, 1, 1],
  "opened_apps": ["Firefox", "LibreOffice", "Files"],
  "current_app": "LibreOffice",
  "timestamp": "2026-09-09 18:00:00",
  "user_group": "通用用户"
}
```

```bash
python3 lzx/service/runtime_monitor/scripts/infer_visit_window.py --input-json input.json
```

输出 `all_probabilities` 包含每个真实应用的 `app_id`、`app`、`p_visit_30s`、`p_visit_180s`、`thermal_state`、`prediction_available`、`predicted_at`、`expires_at` 和 `probability_source`，同时返回 `hot_apps`、`cold_apps`、`neutral_apps`。这里的 `app_id` 是词表 ID；monitor 还会附加独立的 `runtime_app_id`。

仅对后台已运行（运行态打开集合内）的应用分类：30 秒概率 ≥0.90 为 `hot`；180 秒概率 <0.20 为 `cold`；其余为 `neutral`。当前前台为 `foreground`，未运行应用为 `not_running`；缺失或过期的后台预测为 `unavailable`。分类使用全部候选，不截取 Top-K。

## 在线运行与回放

```bash
bash lzx/service/runtime_monitor/scripts/run_visit_window_online.sh \
  --duration 120 --output-dir /tmp/parp-visit-online --session-id visit-demo
```

切换或打开集合变化时立即预测，无事件时每 30 秒刷新。直接事件模式可附加 `--direct-x11-events`；该模式使用事件状态快照作为前台和打开集合的来源，采样时钟仅驱动刷新。新格式被两个旧 bridge 和 MGLRU 写入入口显式拦截，启动参数也会拒绝将此模型与旧内核/回收接口一起启用。

输出为 `model/online_visit_window_predictions.csv`、`review/online_visit_window_calls.csv` 和 `review/visit_window_latest.json`。JSON 中 `snapshot_at` 是生成快照的时间。落盘文件不会自行随时间更新，消费者必须检查 `expires_at`；到达该时刻即失效，不得继续使用旧名单。Python 消费者可通过 runner 的 `current_result(timestamp, current_app, opened_apps)` 获得按查询时刻重新检查过期状态的结果。

无有效历史、模型加载失败或推理错误时，输出明确的不可用状态，不将低值或缺失值默认为冷。事件时间顺序异常会被记录；在一个旧状态周期快照之后延迟送达、但未越过后续真实切换的原生事件仍按真实事件时间处理。

```bash
python3 lzx/service/runtime_monitor/scripts/replay_visit_window.py \
  --output-dir /tmp/parp-visit-replay --sessions 20
```

回放按开始时间选取测试分区最早的完整会话，使用原始时间戳和虚拟 30 秒时钟，不压缩时间，不操作桌面。相同状态分别经过采样和直接事件 runner，对比输入、概率、名单，并根据真实未来进入时间评估。小规模回放用于验证链路，不替代完整独立测试集的效果评估。

## 本次结果与限制

完整数据集共有 468,271 个样本，原始输入逐行核对一致。20 轮训练后第 20 轮验证损失最低。

| 独立测试指标 | 30 秒 | 180 秒 |
|---|---:|---:|
| 有效应用标签数 | 1,037,619 | 1,018,584 |
| 正标签数 | 7,501 | 18,879 |
| PR-AUC（AP） | 0.27619 | 0.33529 |
| Brier 分数 | 0.005890 | 0.014476 |

后台候选共 21,618 个应用样本。热名单为空，热准确率不可评估、召回率为 0；冷名单覆盖率 76.46%，有有效标签的冷候选中 10.24% 在 180 秒内被访问。没有调整约定阈值来改变这一结果。

20 个原始时间会话回放产生 2,785 次预测，包含 2,711 次周期刷新，两个在线路径的最大概率差为 0。该回放仅有 35 个后台候选，不能用其冷热比例代表整体效果。

首次桌面采集试运行未获得白名单应用的有效前台历史，随后进程以 SIGTERM 结束；后续 5 秒诊断运行正常退出，确认采集入口、模型加载及无历史时的不可用输出正常，但仍没有有效前台历史，因此未计为真实桌面概率预测验证成功。原常驻服务未重启。本次完成的概率与刷新验证来自真实 LSApp 时间回放；部署后仍需在实际使用的应用集合上验证映射、时序及阈值效果。本次未验证内核回收收益。

测试入口：

```bash
python3 lzx/service/runtime_monitor/tests/test_visit_window.py
```
