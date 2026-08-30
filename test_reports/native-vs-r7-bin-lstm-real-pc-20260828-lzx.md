# Native vs r7 LSTM + bin-reclaim 真实 PC 配对报告（2026-08-28）

## 结论

前台 LSTM + bin-reclaim 已通过真实 GUI 应用场景中的机制与回收来源验证：预测结果确实经 `/dev/myfs` 下沉并改变了父 memcg direct reclaim 的应用选择，而且没有依靠增加 direct scan 获得结果。

但“降低真实应用 fault/major fault”目前不能判定通过。APPLY 稳定减少了热应用被回收的内存，并使预测应用的 UI 操作耗时小幅下降；回前台 fault 在三个 seeds 间波动较大，predicted-return 的均值反而增加。因此当前版本可以证明“机制有效、回收方向正确”，还不能宣称所有真实 PC 交付指标已经优化。

| 验证项 | 判定 | 结果 |
|---|---|---|
| LSTM 排名与真实应用覆盖 | PASS | 9/9 APPLY 均预测 Firefox Top-1、Thunderbird Top-2；8 个 GUI 应用全部绑定 |
| `/dev/myfs` 下沉 | PASS | 9/9 为 `APPLIED`、ABI v2、无歧义域 |
| bin-reclaim 实际动作 | PASS | 2,251,499 次 lookup、2,251,499 次 policy hit、267,919 次 subtree selected |
| 冷/热回收来源重排 | PASS | 冷应用占比稳定提高约 7.34–7.53 个百分点；热应用内存损失下降 4.16%–6.74% |
| direct scan 不恶化 | PASS | 三场景变化为 +0.414%、-0.184%、+0.096%，基本持平 |
| 预测应用 UI 恢复耗时 | 初步 PASS | Firefox 场景下降 2.15%，3/3 配对均下降；多应用场景下降 0.78% |
| 回前台 fault/major fault | 未通过 | Firefox 场景父 cgroup 均值分别增加 10.15%/10.56%，轮间方差明显 |
| OOM 安全 | PASS | 18 个正式轮次均无 OOM/应用被杀 |

## 实验身份与配对约束

- Native：`6.17.13-native-6.17.13 #2`
- APPLY：`6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7`
- Native 目录：`/home/lzx/Desktop/PARP/test/outputs/real_pc/native_kernel-20260828_174345-6.17.13-native-6.17.13`
- APPLY 目录：`/home/lzx/Desktop/PARP/test/outputs/real_pc/bin_lstm-20260828_180627-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r7`
- 配对：3 个场景 × seeds `20260828/20260829/20260830`，Native 9/9 VALID，APPLY 9/9 VALID
- 实际可见内存：约 15 GiB；swap 约 3.8 GiB
- 素材 manifest SHA-256：`adcf1cb6a3adeffa64bc21d32c36c6468e3f656cb816eb2de701399c25bcf94d`
- 每轮压力：同一测试父 memcg 内分配 1 GiB；动态 `memory.max` 预留 512 MiB headroom；`memory.swap.max=1 GiB`
- 应用工作集：真实浏览器、邮件、媒体、办公、图片、PDF 和游戏进程；没有启动 `memory-fixture-lzx.py`

Native 的 myfs 检查采用 `--no-require-myfs`；APPLY 采用 `--require-myfs`，这只是内核接口有效性门禁，不改变应用序列、素材、压力或计分窗口。最初一次 APPLY 校准沿用了 fixture 场景的 16 bindings 门槛，在压力前被判 INVALID；它不在上述正式 APPLY 目录和统计中。真实 GUI 场景应要求 8 个应用绑定，正式轮次实际均为 9 个绑定（8 个应用 scope 加父策略域）。

## APPLY 运行时状态

9 个 APPLY 正式轮次的 `policy-before.json` 均一致：

```text
parp_mode=2
effective_tier_mode=0
tier2_predict_enabled=0
parp_reclaim_bin_enabled=1
tier2_wss_predict_enabled=0
tier2_wss_strengthen_enabled=0
memory.tier2_enabled=1  # 仅作为父策略域/bin 排序入口，不启用 Tier2 proactive
```

bin-reclaim 九轮合计：

| 内核动作 | 合计 | 每轮均值 | 每轮范围 |
|---|---:|---:|---:|
| lookups | 2,251,519 | 250,168.78 | 244,109–256,998 |
| policy_hits | 2,251,499 | 250,166.56 | 244,107–256,996 |
| context_hits / rank_scores | 1,626,856 | 180,761.78 | 176,233–185,529 |
| subtree_passes | 211,881 | 23,542.33 | 23,034–24,330 |
| subtree_selected | 267,919 | 29,768.78 | 28,858–30,879 |
| subtree_skipped | 1,983,262 | 220,362.44 | 215,213–226,416 |
| rebin_moves | 0 | 0 | 0 |

`rebin_moves=0` 在本场景是预期行为，不代表 bin 没有动作。它只统计全局 MGLRU `shrink_many()` 中旧 FIFO 条目的物理换 bin；本实验对父 memcg 施压，direct reclaim 走 `shrink_node_memcgs()`，有效动作是八轮 `subtree_passes/subtree_selected`。

## 回收来源结果

三个 seeds 的均值：

| 场景 | 指标 | Native | APPLY | 变化 |
|---|---|---:|---:|---:|
| cold-retire | 冷应用来源占比 | 44.930% | 52.266% | **+7.336 pp** |
|  | 热应用回收量 | 176.956 MiB | 166.494 MiB | **-5.91%** |
|  | 冷应用回收量 | 144.358 MiB | 182.322 MiB | +26.30% |
| predicted-return | 冷应用来源占比 | 44.944% | 52.472% | **+7.528 pp** |
|  | 热应用回收量 | 177.445 MiB | 165.488 MiB | **-6.74%** |
|  | 冷应用回收量 | 144.853 MiB | 182.905 MiB | +26.27% |
| mixed-multitask | 冷应用来源占比 | 46.122% | 53.503% | **+7.381 pp** |
|  | 热应用回收量 | 173.947 MiB | 166.704 MiB | **-4.16%** |
|  | 冷应用回收量 | 148.961 MiB | 191.987 MiB | +28.88% |

两个应用集合在压力前的实际内存规模在两种内核间接近：热应用约 623–625 MiB，冷应用约 608–631 MiB。归一化后，Native 会回收约 27.9%–28.4% 的热应用内存、23.6%–23.7% 的冷应用内存；APPLY 改为回收约 26.5%–26.7% 的热应用内存、30.0%–30.5% 的冷应用内存。说明方向翻转来自内核选择，而不是 APPLY 启动时冷应用恰好占用更多内存。

## 扫描、压力与 fault

| 场景 | 指标 | Native | APPLY | 变化 |
|---|---|---:|---:|---:|
| cold-retire | direct scan | 267,136 | 268,242 | +0.414% |
|  | direct steal | 136,007 | 136,179 | +0.126% |
|  | 压力期 major fault | 2,778 | 3,002 | +8.08% |
|  | PSI full avg10 峰值 | 7.567% | 7.720% | +2.03% |
| predicted-return | direct scan | 266,804 | 266,313 | -0.184% |
|  | direct steal | 135,901 | 135,522 | -0.279% |
|  | 压力期 major fault | 2,704 | 2,660 | -1.63% |
|  | PSI full avg10 峰值 | 7.350% | 7.577% | +3.08% |
| mixed-multitask | direct scan | 266,975 | 267,231 | +0.096% |
|  | direct steal | 135,982 | 135,982 | 约 0% |
|  | 压力期 major fault | 2,514 | 2,973 | +18.26% |
|  | PSI full avg10 峰值 | 7.260% | 7.967% | +9.73% |

扫描量基本相同，支持“收益来自应用排序”的判断。不过 APPLY 在 cold-retire 和 mixed-multitask 中增加了压力期 major fault/PSI，这属于需要继续优化的代价。

## 回前台效果

| 场景 | 指标 | Native | APPLY | 变化 |
|---|---|---:|---:|---:|
| predicted-return（Firefox） | 父 cgroup pgfault | 985.0 | 1,085.0 | **+10.15%** |
|  | 父 cgroup major fault | 915.667 | 1,012.333 | **+10.56%** |
|  | Firefox 自身 major fault | 834.333 | 864.667 | +3.64% |
|  | UI 动作耗时 | 923.229 ms | 903.422 ms | **-2.15%** |
| mixed-multitask（三个热应用） | 父 cgroup pgfault | 1,235.333 | 1,215.667 | -1.59% |
|  | 父 cgroup major fault | 1,146.0 | 1,108.0 | -3.32% |
|  | UI 动作耗时 | 1,422.264 ms | 1,411.174 ms | -0.78% |

predicted-return 的三个 UI 配对均改善：`924.126→917.402 ms`、`905.981→875.426 ms`、`939.580→917.440 ms`。但 fault 并不稳定：第三个 seed 的 Native major fault 异常低（721），APPLY 为 1,067，拉高了均值。

mixed-multitask 中，Firefox 自身 major fault 平均下降 12.45%，但 Thunderbird 平均增加 43.17%；说明当前 rank1 保护明显，rank2 保护仍不足，收益在热应用之间发生了转移。这与当前分箱规则一致：rank1/前台进入 bin7，rank2 只进入 bin6。

`workingset_refault_file` 在两边都为 0。真实 GUI 内容主要变成 DOM、图像解码和应用堆等匿名页，本轮回前台代价表现为 swap-in/major fault，而不是 fixture 文件页 refault。

## 下一步

1. 先把正式重复数从 3 增加到至少 10，确认 fault 与 PSI 的置信区间；当前 n=3 只适合作为真实 PC pilot。
2. 做一个只改变 rank2 分箱的消融：将 rank2 从 bin6 提升到 bin7，观察 Thunderbird fault 是否下降，同时检查是否挤压冷应用回收空间。
3. 增加“工作完成时间/掉帧/音频 underrun”等用户感知指标，避免只用窗口按键动作时间代表体验。
4. 大模型 runtime 推理作为独立工作负载和 SHADOW predictor 接入。当前机器没有 Ollama/llama.cpp、本地权重或 GPU，不能把大模型写成已参与本报告；安装后必须重新做独立 Native/APPLY 配对，不能只在 APPLY 一侧启用。
