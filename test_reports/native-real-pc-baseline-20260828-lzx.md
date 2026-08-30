# Linux 6.17.13 Native 真实 PC 使用基线（2026-08-28）

## 当前结论

前台 LSTM + bin-reclaim 的机制验证可以结束，并已进入真实 PC 使用效果验证阶段。本报告只固化 Native 基线；APPLY 必须重启到 r7 内核后，使用相同场景、素材和 seed 配对运行，当前不能提前下结论说真实 PC 效果已经提升。

- 内核：`6.17.13-native-6.17.13 #2`
- 正式结果：9/9 VALID（3 场景 × 3 seeds）
- seeds：`20260828`、`20260829`、`20260830`
- 正式基线目录：`/home/lzx/Desktop/PARP/test/outputs/real_pc/native_kernel-20260828_174345-6.17.13-native-6.17.13`
- 应用工作集：真实 GUI 应用及真实本地内容；没有使用 `memory-fixture-lzx.py` 伪装应用工作集
- 压力源：同一受控 memcg 中的 1 GiB 匿名内存分配，只负责稳定制造内存压力
- OOM：0 个正式轮次发生 OOM/应用被杀；任何 `oom_kill` 增量都会使轮次 INVALID

## 真实应用与使用内容

| 应用 | 自动化内容/动作 | 预测类别 |
|---|---|---|
| Firefox（Epiphany 映射） | 打开并滚动 11.1 MiB 离线长网页 | 热，预计即将复用 |
| Thunderbird | 打开并滚动 3.9 MiB 本地邮件线程 | 热，预计即将复用 |
| VLC | 播放/暂停 90 秒、16.5 MiB 本地音频 | 热 |
| GIMP | 打开并缩放解码后 48 MiB 的 4096×4096 图像 | 冷 |
| LibreOffice Writer | 打开并滚动 15.6 MiB 工程文档 | 冷 |
| Evince | 打开并翻阅 240 页本地 PDF | 冷 |
| Image Viewer | 打开并缩放 4096×4096 图像 | 冷 |
| Solitaire | 启动并执行游戏内操作 | 冷 |

所有应用均在独立的 `automation-<app>.scope` 中运行。训练历史固定为：

`Thunderbird → Firefox → Thunderbird → Firefox → VLC`

9 个正式轮次中 LSTM 输出完全一致：Firefox Top-1 概率 `0.792178`，Thunderbird Top-2 概率 `0.132390`；GIMP、LibreOffice、Evince、Image Viewer、Solitaire 均低于冷应用门槛 `0.01`。

## Native 正式基线指标

以下为三个 seeds 的均值；范围给出轮间最小值–最大值。

| 场景 | 应用内存减少 | 冷应用来源占比 | 热应用来源占比 | direct scan | direct steal | 压力期 major fault | PSI full avg10 峰值 |
|---|---:|---:|---:|---:|---:|---:|---:|
| cold-retire | 321.314 MiB | 44.930% | 55.070% | 267,136 | 136,007 | 2,778 | 7.567% |
| predicted-return | 322.298 MiB | 44.944% | 55.056% | 266,804 | 135,901 | 2,704 | 7.350% |
| mixed-multitask | 322.908 MiB | 46.122% | 53.878% | 266,975 | 135,982 | 2,514 | 7.260% |

回前台阶段：

| 场景 | 回前台 pgfault | 回前台 major fault | file refault | 真实 UI 动作耗时 |
|---|---:|---:|---:|---:|
| predicted-return（Firefox） | 985（754–1,102） | 915.667（721–1,019） | 0 | 923.229 ms（905.981–939.580） |
| mixed-multitask（三个热应用） | 1,235.333（1,208–1,278） | 1,146（1,141–1,154） | 0 | 1,422.264 ms（1,395.505–1,449.498） |

这里 `file refault=0` 不代表没有回前台代价。真实 GUI 应用的文档解析、DOM、图像解码和进程堆主要形成匿名页；本场景在回前台时主要表现为 swap-in 造成的 major fault。因此真实 PC 阶段的主指标应同时看 `pgfault`、`pgmajfault`、`pswpin/pswpout` 和 UI 恢复耗时，不能只看 `workingset_refault_file`。

## Native 基线说明的问题

Native 在约 322 MiB 的应用内存减少量中，只有约 45% 来自五个低概率冷应用，反而约 55% 来自三个高概率热应用。随后预测即将复用的 Firefox 回到前台，平均出现约 916 次 major fault。这正是 bin-reclaim 需要改善的真实 PC 问题：

1. 在完成相近总回收量的前提下，提高冷应用回收来源占比，降低热应用内存减少量。
2. 降低预测应用回前台的 `pgfault`、`pgmajfault`、swap-in 和恢复耗时。
3. 不以更高 direct scan、PSI、OOM 或服务开销换取上述收益。

## 下一步配对门槛

重启 r7 APPLY 后，必须保持相同素材 manifest、三个 seeds、1 GiB 压力、512 MiB 目标回收量和 1 GiB memcg swap 上限。APPLY 轮次还必须满足 `/dev/myfs` ABI v2、预测下沉 `APPLIED`、至少 8 个真实 GUI 应用绑定、`policy_hits > 0`、`subtree_selected > 0`，且 effective-tier、Tier2 proactive、WSS 均保持关闭。这里不要求 `rebin_moves > 0`：该计数只对应全局 `shrink_many()` 的 MGLRU FIFO 物理迁移，而本实验的父 memcg direct reclaim 使用 `shrink_node_memcgs()` 的预测 subtree 排序路径。

大模型 runtime 推理可以加入，但本机当前没有 Ollama/llama.cpp、GPU 或本地大模型权重。为避免污染这组 LSTM + bin-reclaim 因果实验，大模型应作为独立的固定 workload 和 SHADOW predictor 做第二套 Native/APPLY 配对，不能只在 APPLY 一侧临时启用。
