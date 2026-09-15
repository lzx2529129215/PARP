# CODE AUDIT

Phase 1: PASS — 已定位模块；lzx-zr 原先不存在。无适用 AGENTS.md。

| 当前文件 | 当前功能 | 本轮是否修改 / 方式 |
|---|---|---|
| `test/test/prepare_lsapp_30.py` | 原始 LSApp→30 应用，source-first opened 状态 | 只读冻结 |
| `test/test/run_lsapp_30_ablation.py` | 20-history 因果重建、训练评估 | 只读参考，在新目录生成特征 |
| `lzx/tool/operation_predictor/v3/src/data/build_app_dataset_visit_window.py` | p30/p180 标签、时间边界、删失 | 只读；新实现分段标签 |
| `lzx/tool/operation_predictor/v3/models/app_lstm_duration.py` | LSTM、duration、opened、current encoder | 复制后原样复用 |
| `lzx/tool/operation_predictor/v3/models/app_lstm_visit_window.py` | padding 修复与双窗口头 | 复制复用 M0；M1 只替换头 |
| `lzx/tool/operation_predictor/v3/models/app_lstm_visit_explicit.py` | 14 项显式特征、32维分支 | 复制保持编码，只换分支末层 |
| `test/outputs/lsapp-30-training-20260910-v1/h20_explicit/checkpoint.pt` | 当前 M0 checkpoint | 复制冻结，不覆盖 |
| `test/outputs/lsapp-30-data-20260910-v1/dataset` | M0 时间划分、segments、opened | 复制冻结；保留分区边界和原查询索引 |
| `lzx/service/runtime_monitor/predictor_visit_window.py` | 推理契约与概率输出 | 只读审计，Gate 后另写入口 |
| `lzx/service/runtime_monitor/online_visit_window.py` | 切换/采样/定时刷新 | 只读，Gate 后适配 |
| `test/test/visit_window_bin.py` | p180→Q15 prior→myfs | 只读，Gate 后适配 |
| `lzx/service/runtime_monitor/core/parp_myfs.py` | 原子 App prior、rank、memcg binding、TTL | 只读，Gate 后复用 ABI |
| `lzx/kernel/v4.2/patches/0005-reclaim-bin-hierarchy-and-stats.patch` | memcg bin 与证据统计 | 不修改 |
| `lzx/kernel/v4.2/patches/0014-parp-reclaim-bin-restart-lower-bin.patch` | 冷 bin 低编号先扫描 | 不修改 |
| `test/test/visit_window_real_effects.py` | 原有 GUI/压力/回收实验 | 只读；Gate 后才执行独立副本 |

## 审计差异与处理
- 用户后续明确取消目录限制，可按需修改 lzx。当前选择保留独立实验目录与冻结 baseline；尚无必要修改旧模块。
- M0 原始查询为 switch + 180 秒。新增 switch + 30 秒查询按任务要求生成；三方法同查询同候选，另报告原 M0 查询子集，不能声称查询分布未改变。
- 冻结 M0 的精确时间边界，不用新增 query 数量重算 70/15/15。原全局时间切分可能穿过 session；不随机拆分，标签按 partition 截断，保留因果的分区前历史。会话交叉数量由验证报告披露。
- 分段区间左闭右开，恰好 30 秒属于 C1；旧 p30 包含恰好 30 秒。两种标签不能直接等同。
- opened 保留旧片段开始时状态，不悄悄改成新的 resident 估计。LSApp opened state != true PC resident process state。
- 已读内核 patch 显示低编号 bin 为冷、先回收；任务中的 C0热/C7冷是逻辑 segment 编号，不能直接写入物理 bin。Gate 后必须验证 ABI 映射，不能通过修改内核反转顺序。
- M0 explicit 实际为 flatten(32×14)→32 ReLU→64 residual，与 LSTM 输出相加。M1 保持此分支，只将两个末层输出由每应用2项替为8项。App-specific Linear rows 作为候选专属 head，不另加候选 embedding/encoder。
- 本实现为用户指定 SLAP-style，不声称复现某篇未提供全文的 SLAP 论文。
