# 双时间窗口预测的五个 GUI 场景

> 当前版本的 V1–V5 是人工编排的行为场景，尚未按训练分布构建，适用于功能检查与指定行为下的压力检验。2026-09-10 的15轮批次已在完成6轮后停止；其结果不能作为代表训练数据分布的模型准确率结论。后续正式场景应以训练统计确定类型、以独立测试集真实序列构建。

入口为 `test/test/visit_window_scenarios.py`，配置为同目录
`visit-window-scenarios.json`。从 PARP 根目录运行下面的命令。

本入口在原 `test` 框架上扩展，复用：

- `test/test/parp-acceptance-lzx.py`：应用命令、私有配置和测试素材初始化。
- `test/automation/app_automation.py`：systemd scope 启动、窗口验证、键鼠操作和 scope 清理。
- `test/automation/create_real_pc_assets_lzx.py` 与已有素材缓存：网页、文档、邮件、图片、音频、PDF。
- 现有 `OnlineVisitWindowRunner` 和已训练的 `visit_window_v1` checkpoint：实际推理及30秒周期刷新。

不训练新模型，不扩大词表，不修改或重启常驻服务。只记录预测与评估，
不调用 `/dev/myfs`、debugfs 概率写入或内存回收控制。这里的“冷”仅为预测输出。

## 场景与时间

每轮启动6个应用：浏览器、LibreOffice、Thunderbird、VLC、GIMP、Evince，
因此最多5个后台应用。沿用原测试映射：实际浏览器为 Epiphany，映射到模型的 Firefox。
词表仍为15个真实应用，另有2个特殊项；未运行的词表应用只输出概率。

| 场景 | 实际操作 | 检查目标 |
|---|---|---|
| V1 `v1_short_return` | 浏览器与文档交替，每次停留10/15/20秒 | 频繁切换时的30秒概率与热名单 |
| V2 `v2_cold_retire` | 浏览器与文档每20秒切换，其余4个应用保持后台 | 长时间不用的应用是否进入冷名单 |
| V3 `v3_medium_return` | 在计分第90秒、第330秒进入邮件；其余时间浏览器和文档交替 | 相对第0秒和第180秒，分别延迟90秒和150秒返回 |
| V4 `v4_staggered_return` | 第15/60/120/210秒分别进入文档/邮件/视频/图片，PDF保持后台 | 多个后台应用在不同时间返回 |
| V5 `v5_active_set_shift` | 前180秒交替浏览器与文档，后180秒交替图片与PDF | 活跃应用集合变化后的预测响应 |

默认每场景3轮、串行执行。每轮60秒预热（各应用10秒）、360秒计分、181秒后续观察。
后续观察保持最后一个应用前台，不继续切换。15轮净时长9015秒（约2小时30分钟），
另加应用启动和清理时间。所有时间均为真实秒数，**不压缩停留时间**。
预热、后续观察中的进入事件参与构建历史或补全标签，其预测不计入计分指标。

冷热身份完全来自当前模型：运行后台应用 `p30 >= 0.90` 为 hot，
`p180 < 0.20` 为 cold，其余 neutral；前台为 foreground。
场景名称与操作计划不会作为冷热名单，也不会传入预测输入。
没有预测热应用时如实报告空名单，准确率为 N/A，不降低阈值。

## 运行

```bash
# 只生成操作计划，不启动应用
python3 test/test/visit_window_scenarios.py plan \
  --output-dir test/outputs/visit-window-plan

# 验证依赖、隔离桌面、checkpoint 和 systemd user scope
python3 test/test/visit_window_scenarios.py preflight \
  --output-dir test/outputs/visit-window-preflight

# 短冒烟：真实12秒预热、35秒计分、35秒后续观察
python3 -u test/test/visit_window_scenarios.py smoke \
  --output-dir test/outputs/visit-window-smoke

# 正式五场景，每场景3轮
python3 -u test/test/visit_window_scenarios.py run \
  --output-dir test/outputs/visit-window-formal

# 单个场景、单轮，仍使用完整真实时间窗口
python3 -u test/test/visit_window_scenarios.py run \
  --scenario v3_medium_return --rounds 1 \
  --output-dir test/outputs/visit-window-v3-single

python3 -m unittest discover -s test/test/tests -p test_visit_window_scenarios.py -v
```

输出目录必须不存在，避免覆盖旧结果。`--checkpoint` 可指定兼容的双窗口 checkpoint，
加载时沿用现有模型的格式与词表校验。

默认 `--display-mode isolated` 创建本轮套件专用的 Xvfb＋Openbox 桌面，运行真实应用GUI。
需要 `Xvfb`、`openbox`、`xdotool`、`wmctrl`、systemd user manager 以及上述应用。
`--display-mode host` 可使用原桌面，但锁屏或用户操作可能导致窗口校验失败。
隔离桌面不是模拟预测或事件回放；前台变化由 X11 实时读取。

## 观测、归属和异常处理

独立观测线程每0.1秒读取本轮应用窗口和当前前台。应用切换或运行集合变化触发事件路径，
无变化时调用采样路径，由现有预测器决定是否到达30秒刷新时刻。
输入只含已经观测到的历史、当前停留时长、运行集合和当前时间，不读取计划中的未来动作。

每轮使用独有的素材、浏览器/邮件/文档/GIMP配置目录及 scope 名称。
常驻服务可能把测试进程迁出原 scope，因此在 scope 之外，结合可执行文件与本轮私有路径识别应用。
清理时记录 PID 与启动时间，终止已识别的本轮进程及其后代，并停止本轮 scope；
不按通用进程名全局清理。`ownership.json` 记录原 scope、实际 cgroup、PID 与启动时间。

主窗口标题必须对应本轮内容，激活后必须验证同一个窗口；仅出现恢复/提示窗口不算准备完成。
启动缺失窗口、推理失败、操作延迟超过3秒、采样间隔超过1秒或累计未知前台超过2秒，
该轮标记 INVALID 并停止套件。不会因为预测准确率低或热名单为空而重跑。

## 标签与指标

每次计分预测，以观测时刻 `t` 为锚点，分别检查 `(t,t+30]`、`(t,t+180]` 内的实际前台进入事件。
锚点本身的进入不计入未来；持续停留不产生新进入，离开后返回当前应用计入。
同一窗口多次进入仍是一个二元正标签。标签使用单调时钟，模型时间特征使用实际本地时间。
前台事件时间是轮询首次观测时间，存在采样延迟；每轮报告最大采样间隔。

已观察到进入则为正；观察不足且未进入则掩码排除。正式轮次的181秒后续观察覆盖计分末尾的180秒窗口。
smoke 不具备完整180秒观察，结果只用于链路验证，不进入正式汇总。

窗口 PR-AUC（AP）与 Brier 覆盖词表全部真实应用；冷热指标只覆盖当时运行的后台应用。
冷实际访问率 = 180秒内实际进入的有效冷应用-预测时刻对 / 全部有效冷应用-预测时刻对。
例如100个有效冷判断中有10个后来被进入，就是10%，不是“每次必有一个冷应用返回”。
同时报告每次冷应用数分布、名单覆盖率及有效标签数。人工场景指标不替代 LSApp 独立测试集指标。

## 产物

套件目录包含 `plans.json`、`config.json`、`desktop.json`、`preflight.json`（checkpoint哈希）、
`progress.json`、`summary.json` 与 `REPORT.md`。

每轮目录包含：

- `scenario.json`、`asset_hashes.json`、`owned_scopes.json`、`ownership.json`：计划与归属证据。
- `verified_actions.json`：计划时间、实际激活时间、窗口、PID及操作延迟。
- `observations.jsonl`、`foreground_entries.json`：实际前台与进入事件。
- `prediction_bundles.jsonl`：每次预测完整输出及当时的观测状态。
- `model/online_visit_window_predictions.csv`：应用级概率、状态、生成/失效时间和概率来源。
- `review/online_visit_window_calls.csv`、`review/visit_window_latest.json`：调用历史和最后快照。
- `scored_predictions.npz`、`evaluation.json`、`result.json`：计分概率、标签、掩码和指标。
