# FINAL-OFFLINE-REPORT

**Gate-2：FAIL。本轮到此 STOP；runtime/kernel 未修改。**

当前证据不足以认定 M2 比现有 p180 更适合 App reclaim priority，尚不值得将这一版 M2 接入真实内核实验。预测时间 → risk/coldness → priority/bin 的解耦已实现并可独立测试；架构解耦成立不等于预测收益达到门禁。

## 1. 实施范围与数据单位

仅在 `lzx-zr/experiments/reentry_priority_v2/` 新增离线实现；复用已有 LSTM encoder、既有 p180/M1 checkpoint 和冻结时间划分。没有训练 8-class CE 的新模型，没有导入或写入 runtime sink、内核接口。配置、数据审计、训练历史、checkpoint、完整评估支持数组均保留。

| 划分 | 原 query 数 | 保留 query 数 | 候选行 | episodes | 有监督 episodes | 排除返回后陈旧行 | 每 episode 最多 query |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | 2147023 | 813202 | 1111417 | 59714 | 59117 | 214 | 488 |
| val | 452167 | 168257 | 235290 | 13867 | 13754 | 97 | 422 |
| test | 464339 | 171526 | 245895 | 14562 | 14430 | 114 | 460 |

episode 键为 `(session, app, last_departure, observation_end)`。相同时间戳保持原始事件顺序；候选必须处于离开之后、下一次返回之前。每个 episode 的所有 query 权重和为 1，包括 mask 全零的 query；后者 loss=0，不伪造标签。one-query-per-episode 每 epoch 在该 episode 的全部 query 中均匀随机取一条。两种模式各训练一份模型。

跨划分的物理 episode 数：{'train_val': 13, 'val_test': 4, 'train_test': 0}。这些 episode 在分区边界做行政删失，分区内分别加权，训练标签不跨验证/测试边界。原历史数据划分已在此前实验中使用，明确视为开发评估，不重新命名为未触碰 holdout。

## 2. M2 与独立优先级层

LSTM 与显式特征分支保持既有结构；仅终端输出变为 5 个 survival logits。`z0` 自由，后续 `zk=z0−Σsoftplus(dj)`，因此 P(T>t) 随 t 单调不增。阈值为 3/10/30/60/180 秒。使用 masked BCE，每个 query 先对已知阈值求均值，再应用 episode 权重。T=47 标签为 [1,1,1,0,0]；删失下界75秒的 mask 为 [1,1,1,1,0]。

risk30=1−S(30)，risk180=1−S(180)。coldness 为 [0,180] 上 survival 曲线的梯形积分除以180（截断平均存活时间）；不声称预测180秒以后的实际平均返回时间。uncertainty 是 5 个 Bernoulli 概率的平均归一化熵，仅诊断，不参与 mapper 或门禁。

RankOnly 按 coldness 的相对秩映射到 Bin0–Bin7；Bin0 热、Bin7 冷。RiskAwareRank 用预先固定的 risk30≥0.5 或 risk180≥0.8 将高风险候选放到 Bin0–Bin1，其余按 coldness 放到 Bin2–Bin7。模型不返回 bin，mapper 不读取训练时间类别。不同预测阈值数量仍使用相同 8-bin mapper，单元测试已验证。相同分数同 bin；回收选择在 bin 并列时按 app_id 固定打破并列，POA 并列计0.5。

## 3. 训练与选择

| 采样 | 实际 epochs | 优化步数 | 最佳 epoch | 验证 episode BCE | 训练秒 |
| --- | --- | --- | --- | --- | --- |
| episode_weighted | 13 | 5174 | 9 | 0.43306 | 1993.95338 |
| one_query_per_episode | 35 | 1015 | 27 | 0.43633 | 568.26988 |

M2 从头训练，复用 encoder 架构而非冻结或迁移已训练权重。两模型同 seed=42、Adam lr=0.001、batch=2048、CPU 单线程；weighted 最多20 epoch、至少6 epoch、4轮无改善早停；one-query 最多80 epoch、至少12 epoch、8轮无改善早停。one-query 每轮优化步数约小14倍，因此在测试评估前根据验证曲线扩大其预算，并从相同seed重跑。两模式不是等优化步数对照，实际步数如表。以验证 BCE 选择 checkpoint，未使用测试指标调参。单 seed 的优化方差未被用户 bootstrap 覆盖。当前 p180/M1 使用已有 checkpoint，本轮比较回答最终方案是否更好，不将收益全部归因于 ordinal head。

## 4. LSApp 离线主指标

所有 B0–B4 使用完全相同的 query、后台候选和删失边界。Episode POA 先在同一 episode-pair 内平均重复 query 的排序正确率，再对唯一 episode-pair 求平均；分数并列0.5，真实时间并列不比较，两个无法证明次序的删失候选不比较。其他 headline 指标每个 episode 只取第一个可用决策点，多 episode 同一决策仍按 focal episode 计，每个 episode 一票。主表限至少2候选；query-row POA 仅辅助。

Cold Precision@K 要求真实 Top-K 可确定且候选数>K；DVR 使用 T≤30/T≤180，可判定才计；删失下界不足不当成安全。Victim/EVR 仅在返回时间或最早返回可识别时统计；victim median 是已观察返回的条件中位数，需结合分母看待，不能把删失当无穷大。Gate 比较仅用两方法共同已知结果。

### episode_weighted

| 指标 | B0 Recency | B1 p180 | B2 flat M1 | B3 M2 RankOnly | B4 M2 RiskAwareRank |
| --- | --- | --- | --- | --- | --- |
| Episode POA | 0.66906 | 0.69791 | 0.66763 | 0.66747 | 0.66229 |
| Cold Precision@1 | 0.55731 (n=5453) | 0.60040 (n=5453) | 0.59398 (n=5453) | 0.56959 (n=5453) | 0.56904 (n=5453) |
| Cold Precision@3 | 0.80779 (n=685) | 0.80681 (n=685) | 0.78443 (n=685) | 0.77859 (n=685) | 0.77324 (n=685) |
| DVR@30 ↓ | 0.10643 (n=5816) | 0.11008 (n=5823) | 0.14774 (n=5821) | 0.13383 (n=5821) | 0.13400 (n=5821) |
| DVR@180 ↓ | 0.22319 (n=5762) | 0.19754 (n=5761) | 0.24848 (n=5767) | 0.24189 (n=5763) | 0.24223 (n=5763) |
| Victim median s ↑ | 727.00000 (n=4891) | 821.00000 (n=4740) | 686.50000 (n=4672) | 736.00000 (n=4851) | 734.00000 (n=4852) |
| EVR@2 median s ↑ | 147.00000 (n=5622) | 148.00000 (n=5592) | 135.00000 (n=5583) | 134.00000 (n=5612) | 132.00000 (n=5613) |
| EVR@4 median s ↑ | 57.00000 (n=695) | 58.00000 (n=695) | 55.00000 (n=692) | 55.00000 (n=693) | 54.50000 (n=694) |
| First-anchor POA | 0.64131 | 0.67268 | 0.65161 | 0.63499 | 0.63177 |

辅助 query-row 诊断（不作为 headline 或门禁依据）：

| 方法 | Query-row POA | 唯一 episode pairs | episode decisions | bin/score 并列比例 |
| --- | --- | --- | --- | --- |
| B0 Recency | 0.55294 | 8577 | 5891 | 0.01971 |
| B1 p180 | 0.62212 | 8577 | 5891 | 0.00000 |
| B2 flat M1 | 0.66908 | 8577 | 5891 | 0.00000 |
| B3 M2 RankOnly | 0.62576 | 8577 | 5891 | 0.00000 |
| B4 M2 RiskAwareRank | 0.62506 | 8577 | 5891 | 0.00629 |

### one_query_per_episode

| 指标 | B0 Recency | B1 p180 | B2 flat M1 | B3 M2 RankOnly | B4 M2 RiskAwareRank |
| --- | --- | --- | --- | --- | --- |
| Episode POA | 0.66906 | 0.69791 | 0.66763 | 0.68156 | 0.67597 |
| Cold Precision@1 | 0.55731 (n=5453) | 0.60040 (n=5453) | 0.59398 (n=5453) | 0.57986 (n=5453) | 0.57950 (n=5453) |
| Cold Precision@3 | 0.80779 (n=685) | 0.80681 (n=685) | 0.78443 (n=685) | 0.80097 (n=685) | 0.79513 (n=685) |
| DVR@30 ↓ | 0.10643 (n=5816) | 0.11008 (n=5823) | 0.14774 (n=5821) | 0.12521 (n=5822) | 0.12539 (n=5822) |
| DVR@180 ↓ | 0.22319 (n=5762) | 0.19754 (n=5761) | 0.24848 (n=5767) | 0.22806 (n=5766) | 0.22841 (n=5766) |
| Victim median s ↑ | 727.00000 (n=4891) | 821.00000 (n=4740) | 686.50000 (n=4672) | 768.00000 (n=4836) | 768.00000 (n=4838) |
| EVR@2 median s ↑ | 147.00000 (n=5622) | 148.00000 (n=5592) | 135.00000 (n=5583) | 144.00000 (n=5613) | 140.00000 (n=5615) |
| EVR@4 median s ↑ | 57.00000 (n=695) | 58.00000 (n=695) | 55.00000 (n=692) | 56.00000 (n=694) | 55.00000 (n=694) |
| First-anchor POA | 0.64131 | 0.67268 | 0.65161 | 0.64429 | 0.64103 |

辅助 query-row 诊断（不作为 headline 或门禁依据）：

| 方法 | Query-row POA | 唯一 episode pairs | episode decisions | bin/score 并列比例 |
| --- | --- | --- | --- | --- |
| B0 Recency | 0.55294 | 8577 | 5891 | 0.01971 |
| B1 p180 | 0.62212 | 8577 | 5891 | 0.00000 |
| B2 flat M1 | 0.66908 | 8577 | 5891 | 0.00000 |
| B3 M2 RankOnly | 0.62729 | 8577 | 5891 | 0.00000 |
| B4 M2 RiskAwareRank | 0.62665 | 8577 | 5891 | 0.00689 |

B3/B4 主表使用实际 bin 优先级，不能拿未量化的连续 coldness 冒充内核最终优先级；B0–B2 为现有基线排序。因此 M2 的量化并列损失也包含在比较中。

Mapper 诊断（同一候选集合）：

| 采样 | mapper | Bin0..7候选行占用（仅诊断） | 相对连续coldness改变victim比例 |
| --- | --- | --- | --- |
| episode_weighted | rank_bins | [171526, 982, 3414, 13392, 3414, 982, 0, 52185] | 0.00000 |
| episode_weighted | risk_bins | [14072, 2243, 161447, 3908, 12233, 3908, 39, 48045] | 0.00015 |
| one_query_per_episode | rank_bins | [171526, 982, 3414, 13392, 3414, 982, 0, 52185] | 0.00000 |
| one_query_per_episode | risk_bins | [14914, 2586, 160577, 3893, 12201, 3893, 41, 47790] | 0.00017 |

失败诊断：两种采样下 M2 的 Episode POA 均低于 p180，DVR@30/180 均更高；one-query 的结果较 weighted 好，但仍未达到基线。RankOnly 在本候选集合中没有产生排序并列或改变连续 coldness 的 victim，因此其性能下降不能归因于 8-bin 量化。RiskAwareRank 增加少量并列，未改善结果。当前证据指向本版预测与 coldness 排序方案不足，无法仅据此区分训练目标、时间尺度、特征或优化各自的贡献；按 FAIL→STOP 要求，本轮不再调参重训。

## 5. A11y-CUA 外部验证

仅 SU1–SU8 共480 session；所有模型冻结，A11y 不训练、不选择 checkpoint、不调 mapper。外部验证使用连续 risk/coldness 与 3/10/30/60/180 秒，不强行映射8-bin。

### functional

| 项目 | 数值 |
| --- | --- |
| events | 97022 |
| source_identities | 16 |
| sessions | 480 |
| observed_proxy_episodes | 448 |
| censored_proxy_episodes | 508 |
| mixed_label_episode_ids | 0 |
| mapped_identities | 16 |
| raw_sequence_reentry | 1333 |
| queries | 956 |
| episodes | 956 |
| proxy_collision_queries | 0 |

| 真实身份 | 预测槽位 ID | 预测槽位 App |
| --- | --- | --- |
| Calculator | 8 | Calculator |
| Evince | 6 | Evince |
| Falkon | 15 | Falkon |
| Files | 7 | Files |
| Firefox | 0 | Firefox |
| GIMP | 3 | GIMP |
| GNOMEClocks | 25 | GNOMEClocks |
| GNOMEControlCenter | 29 | GNOMEControlCenter |
| GNOMESoftware | 22 | GNOMESoftware |
| Konqueror | 16 | Konqueror |
| LibreOffice | 1 | LibreOffice |
| Marble | 27 | Marble |
| Pidgin | 17 | Pidgin |
| Shotwell | 12 | Shotwell |
| SystemMonitor | 13 | SystemMonitor |
| VLC | 2 | VLC |

episode_weighted

| 方法 | Episode POA | CP@1 | DVR@30 | DVR@180 | Victim median s |
| --- | --- | --- | --- | --- | --- |
| B0 Recency | 0.68000 | 0.65625 | 0.45763 | 0.96667 | 6.40511 |
| B1 p180 | 0.60000 | 0.45312 | 0.58730 | 0.97727 | 12.24524 |
| B2 flat M1 | 0.62000 | 0.48438 | 0.56250 | 0.97674 | 11.42549 |
| M2 continuous coldness | 0.54500 | 0.48438 | 0.60870 | 0.97872 | 5.86734 |

| 阈值秒 | 可判定候选行 | episode-weighted Brier |
| --- | --- | --- |
| 3 | 1064 | 0.15812 |
| 10 | 903 | 0.26641 |
| 30 | 708 | 0.34876 |
| 60 | 590 | 0.37859 |
| 180 | 505 | 0.35093 |

one_query_per_episode

| 方法 | Episode POA | CP@1 | DVR@30 | DVR@180 | Victim median s |
| --- | --- | --- | --- | --- | --- |
| B0 Recency | 0.68000 | 0.65625 | 0.45763 | 0.96667 | 6.40511 |
| B1 p180 | 0.60000 | 0.45312 | 0.58730 | 0.97727 | 12.24524 |
| B2 flat M1 | 0.62000 | 0.48438 | 0.56250 | 0.97674 | 11.42549 |
| M2 continuous coldness | 0.55500 | 0.53125 | 0.59375 | 0.97674 | 7.72393 |

| 阈值秒 | 可判定候选行 | episode-weighted Brier |
| --- | --- | --- |
| 3 | 1064 | 0.15172 |
| 10 | 903 | 0.24362 |
| 30 | 708 | 0.30082 |
| 60 | 590 | 0.29557 |
| 180 | 505 | 0.23524 |

### process

| 项目 | 数值 |
| --- | --- |
| events | 91977 |
| source_identities | 25 |
| sessions | 480 |
| full_native_episodes | 1592 |
| full_native_observed | 986 |
| full_native_censored | 606 |
| full_native_return_max_s | 339.96712 |
| observed_proxy_episodes | 701 |
| censored_proxy_episodes | 447 |
| mixed_label_episode_ids | 0 |
| mapped_identities | 14 |
| raw_sequence_reentry | 986 |
| queries | 1148 |
| episodes | 1148 |
| proxy_collision_queries | 82 |

| 真实身份 | 预测槽位 ID | 预测槽位 App |
| --- | --- | --- |
| chrome.exe | 15 | Falkon |
| excel.exe | 1 | LibreOffice |
| explorer.exe | 7 | Files |
| msedge.exe | 15 | Falkon |
| mspaint.exe | 3 | GIMP |
| notepad.exe | 1 | LibreOffice |
| onenote.exe | 1 | LibreOffice |
| photos.exe | 12 | Shotwell |
| powerpnt.exe | 1 | LibreOffice |
| slack.exe | 17 | Pidgin |
| taskmgr.exe | 13 | SystemMonitor |
| vlc.exe | 2 | VLC |
| winword.exe | 1 | LibreOffice |
| wmplayer.exe | 2 | VLC |

episode_weighted

| 方法 | Episode POA | CP@1 | DVR@30 | DVR@180 | Victim median s |
| --- | --- | --- | --- | --- | --- |
| B0 Recency | 0.57746 | 0.53514 | 0.72353 | 1.00000 | 9.37471 |
| B1 p180 | 0.39906 | 0.36757 | 0.77596 | 1.00000 | 8.54456 |
| B2 flat M1 | 0.43192 | 0.43784 | 0.75543 | 1.00000 | 8.87199 |
| M2 continuous coldness | 0.43662 | 0.41622 | 0.75000 | 1.00000 | 8.63275 |

| 阈值秒 | 可判定候选行 | episode-weighted Brier |
| --- | --- | --- |
| 3 | 1346 | 0.13404 |
| 10 | 1147 | 0.28103 |
| 30 | 1020 | 0.40646 |
| 60 | 934 | 0.46756 |
| 180 | 857 | 0.46760 |

one_query_per_episode

| 方法 | Episode POA | CP@1 | DVR@30 | DVR@180 | Victim median s |
| --- | --- | --- | --- | --- | --- |
| B0 Recency | 0.57746 | 0.53514 | 0.72353 | 1.00000 | 9.37471 |
| B1 p180 | 0.39906 | 0.36757 | 0.77596 | 1.00000 | 8.54456 |
| B2 flat M1 | 0.43192 | 0.43784 | 0.75543 | 1.00000 | 8.87199 |
| M2 continuous coldness | 0.43528 | 0.43784 | 0.78571 | 1.00000 | 9.04665 |

| 阈值秒 | 可判定候选行 | episode-weighted Brier |
| --- | --- | --- |
| 3 | 1346 | 0.12895 |
| 10 | 1147 | 0.26563 |
| 30 | 1020 | 0.37125 |
| 60 | 934 | 0.38066 |
| 180 | 857 | 0.32807 |

**Process-level 是外部验证的优先口径。** 本报告的 process-level 指 executable/application 字段，不是 PID 生命周期。原生进程身份从 window.application 提取并保留；Word/Excel/PowerPoint 等不合并成一次 App 停留。冻结的30-App模型需要明确的 process→词表槽位适配，因此不同进程可能共用同一预测槽位，候选与真值仍保持分开，碰撞数如上；无法唯一映射的 ApplicationFrameHost 和无对应项保留为观察屏障。完整原生 process reentry 另存 CSV，覆盖所有进程，不受预测槽位限制。这里是有损 proxy transfer，不是经过原生进程词表训练的预测器；不能将其结果当作已解决真实 PC identity 泛化。

## 6. Gate-2

外部验证诊断：process 口径下 M2 的 POA 高于迁移后的 p180，但仍低于 Recency；functional 口径下则低于 p180。原生已观察 process episodes 为986，适配模型可评估701（71.1%），且存在共享预测槽位的碰撞。这些结果不支持稳定的跨域优势，不能推翻 LSApp Gate 的失败。

门禁在训练前固定：按用户成组的配对 bootstrap 2000 次；POA 差值95%CI下界>0；DVR@30/180 点估计不增加且CI上界≤0（零非劣界）；CP@1、CP@3或 victim median 至少一项CI下界>0；first-anchor POA 也必须CI下界>0。最后要求同一 mapper 在两种采样训练下都通过，避免依赖 periodic query 权重。

### episode_weighted / RankOnly：FAIL

| 条件 | 通过 |
| --- | --- |
| poa | False |
| dvr30 | False |
| dvr180 | False |
| other | False |
| no_periodic_dependence | False |

| 指标 | 共同支持 n | 用户数 | p180 | M2 | 差值 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- |
| episode_poa | 8577 | 44 | 0.69791 | 0.66747 | -0.03044 | [-0.037783065568709404, -0.012482780959648996] |
| first_anchor_poa | 5651 | 44 | 0.67268 | 0.63499 | -0.03769 | [-0.04577730588114423, -0.023383350239202207] |
| cp1 | 5453 | 44 | 0.60040 | 0.56959 | -0.03081 | [-0.05932263262694764, -0.012973392303472154] |
| cp3 | 685 | 18 | 0.80681 | 0.77859 | -0.02822 | [-0.03830504267018929, -0.01252348265895944] |
| dvr30 | 5820 | 46 | 0.10962 | 0.13368 | 0.02405 | [0.002752470135343786, 0.03883350329684127] |
| dvr180 | 5756 | 46 | 0.19684 | 0.24097 | 0.04413 | [0.007873270304290149, 0.0723735305897494] |
| victim_time | 4612 | 41 | 822.00000 | 756.50000 | -65.50000 | [-221.0125, -31.0] |
| evr2 | 5575 | 44 | 148.00000 | 136.00000 | -12.00000 | [-25.0125, -1.0] |
| evr4 | 693 | 18 | 58.00000 | 55.00000 | -3.00000 | [-10.0, 0.0] |

### episode_weighted / RiskAwareRank：FAIL

| 条件 | 通过 |
| --- | --- |
| poa | False |
| dvr30 | False |
| dvr180 | False |
| other | False |
| no_periodic_dependence | False |

| 指标 | 共同支持 n | 用户数 | p180 | M2 | 差值 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- |
| episode_poa | 8577 | 44 | 0.69791 | 0.66229 | -0.03562 | [-0.043883167725563166, -0.017246065466111557] |
| first_anchor_poa | 5651 | 44 | 0.67268 | 0.63177 | -0.04091 | [-0.0491858551816394, -0.025627480112791576] |
| cp1 | 5453 | 44 | 0.60040 | 0.56904 | -0.03136 | [-0.05957534651494928, -0.013563767460391608] |
| cp3 | 685 | 18 | 0.80681 | 0.77324 | -0.03358 | [-0.047445157838416165, -0.015503275973792455] |
| dvr30 | 5820 | 46 | 0.10962 | 0.13385 | 0.02423 | [0.0030935647536952378, 0.03887421254664637] |
| dvr180 | 5756 | 46 | 0.19684 | 0.24131 | 0.04448 | [0.008034708728298088, 0.07291488422067391] |
| victim_time | 4612 | 41 | 822.00000 | 756.00000 | -66.00000 | [-221.0125, -31.975000000000136] |
| evr2 | 5576 | 44 | 148.00000 | 133.00000 | -15.00000 | [-29.0, -0.5] |
| evr4 | 694 | 18 | 58.00000 | 54.50000 | -3.50000 | [-12.0, 0.0] |

### one_query_per_episode / RankOnly：FAIL

| 条件 | 通过 |
| --- | --- |
| poa | False |
| dvr30 | False |
| dvr180 | False |
| other | False |
| no_periodic_dependence | False |

| 指标 | 共同支持 n | 用户数 | p180 | M2 | 差值 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- |
| episode_poa | 8577 | 44 | 0.69791 | 0.68156 | -0.01635 | [-0.03214898876956172, -0.005947850199919161] |
| first_anchor_poa | 5651 | 44 | 0.67268 | 0.64429 | -0.02839 | [-0.05694883959246016, -0.009053781310523383] |
| cp1 | 5453 | 44 | 0.60040 | 0.57986 | -0.02054 | [-0.07029895849273765, 0.01857393990268887] |
| cp3 | 685 | 18 | 0.80681 | 0.80097 | -0.00584 | [-0.023668733697524487, 0.02320382032679008] |
| dvr30 | 5820 | 46 | 0.10962 | 0.12491 | 0.01529 | [0.005045212355288093, 0.023790635114132798] |
| dvr180 | 5756 | 46 | 0.19684 | 0.22672 | 0.02988 | [0.013148752726672978, 0.03965958734565567] |
| victim_time | 4610 | 41 | 821.00000 | 782.00000 | -39.00000 | [-311.0875, -6.0] |
| evr2 | 5574 | 44 | 148.00000 | 145.00000 | -3.00000 | [-9.0, 0.0] |
| evr4 | 694 | 18 | 58.00000 | 56.00000 | -2.00000 | [-8.5125, 0.0] |

### one_query_per_episode / RiskAwareRank：FAIL

| 条件 | 通过 |
| --- | --- |
| poa | False |
| dvr30 | False |
| dvr180 | False |
| other | False |
| no_periodic_dependence | False |

| 指标 | 共同支持 n | 用户数 | p180 | M2 | 差值 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- |
| episode_poa | 8577 | 44 | 0.69791 | 0.67597 | -0.02194 | [-0.04039904924989752, -0.010081342536732176] |
| first_anchor_poa | 5651 | 44 | 0.67268 | 0.64103 | -0.03165 | [-0.0615694606166323, -0.011366979299715104] |
| cp1 | 5453 | 44 | 0.60040 | 0.57950 | -0.02091 | [-0.0710013203209396, 0.018305122994301724] |
| cp3 | 685 | 18 | 0.80681 | 0.79513 | -0.01168 | [-0.03405425219941342, 0.01432690431442634] |
| dvr30 | 5820 | 46 | 0.10962 | 0.12509 | 0.01546 | [0.005179969917297179, 0.024017563788966134] |
| dvr180 | 5756 | 46 | 0.19684 | 0.22707 | 0.03023 | [0.013581427739896871, 0.039990889152185204] |
| victim_time | 4611 | 41 | 821.00000 | 782.00000 | -39.00000 | [-322.0, -6.0] |
| evr2 | 5570 | 44 | 148.00000 | 141.00000 | -7.00000 | [-15.0, 0.0] |
| evr4 | 694 | 18 | 58.00000 | 55.00000 | -3.00000 | [-10.0, 0.0] |

**最终 Gate-2：FAIL。通过 mapper：[]。STOP。**

## 7. 产物、验证与复现

数据与 checkpoint 独立验证：PASS。检查每个 episode 权重和=1、返回端点一致、观察/删失 episode 不混合、单调 survival、risk30≤risk180、checkpoint 重放一致。单元测试覆盖标签边界、删失 mask 梯度、权重重复不变性、每 episode 抽样、mapper 风险保护和阈值数/bin数独立。

```bash
cd /home/lzx/Desktop/PARP/lzx-zr/experiments/reentry_priority_v2
python3 build_dataset.py
python3 train.py episode_weighted
python3 train.py one_query_per_episode
python3 evaluate.py
python3 a11y.py
python3 verify.py
python3 gate.py
python3 report.py
```

Runtime/kernel 保护检查：评估前后既有 git diff 哈希一致=True；没有覆盖用户原有修改。

源码：`model.py`、`priority.py`、`build_dataset.py`、`train.py`、`evaluate.py`、`a11y.py`。机器可读：`outputs/dataset/audit.json`、各模式 `history.json`/`checkpoint.pt`/`risk-priority-test.npz`/`episode-metric-support.npz`、`outputs/a11y/validation.json`、`outputs/gate2.json`、`outputs/verification.json`。没有启动 Native MGLRU/p180-bin/M1-bin/M2 内核对照，也没有声称测得 reclaimed bytes、refault、fault、swap-in、PSI、reentry latency 等内核收益。
