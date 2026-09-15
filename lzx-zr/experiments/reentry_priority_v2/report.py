from common import *
import collections

def main():
 g=json.loads((OUT/'gate2.json').read_text());a=json.loads((OUT/'a11y/validation.json').read_text());off=json.loads((OUT/'offline.json').read_text());data=json.loads((OUT/'dataset/audit.json').read_text());verification=json.loads((OUT/'verification.json').read_text());lines=[]
 def p(s=''):lines.append(s)
 def fmt(v):return 'N/A' if v is None else f'{v:.5f}' if isinstance(v,float) else str(v)
 def table(headers,rows):
  p('| '+' | '.join(headers)+' |');p('| '+' | '.join(['---']*len(headers))+' |')
  for row in rows:p('| '+' | '.join(fmt(x) for x in row)+' |')
  p()
 p('# FINAL-OFFLINE-REPORT');p();p(f"**Gate-2：{g['status']}。本轮到此 STOP；runtime/kernel 未修改。**");p()
 if g['status']=='FAIL':p('当前证据不足以认定 M2 比现有 p180 更适合 App reclaim priority，尚不值得将这一版 M2 接入真实内核实验。预测时间 → risk/coldness → priority/bin 的解耦已实现并可独立测试；架构解耦成立不等于预测收益达到门禁。')
 else:p('M2 在预先固定的 episode-level 门禁下优于 p180，解耦设计值得进入下一轮受控真实内核实验。本轮仍按要求 STOP；当前结果来自已反复使用的 LSApp 开发时间段，不能代替未见真实 PC 数据与内核性能验证。')
 p();p('## 1. 实施范围与数据单位');p()
 p('仅在 `lzx-zr/experiments/reentry_priority_v2/` 新增离线实现；复用已有 LSTM encoder、既有 p180/M1 checkpoint 和冻结时间划分。没有训练 8-class CE 的新模型，没有导入或写入 runtime sink、内核接口。配置、数据审计、训练历史、checkpoint、完整评估支持数组均保留。');p()
 table(['划分','原 query 数','保留 query 数','候选行','episodes','有监督 episodes','排除返回后陈旧行','每 episode 最多 query'],[(s,data[s]['original_queries'],data[s]['retained_queries'],data[s]['candidate_rows'],data[s]['episodes'],data[s]['supervised_episodes'],data[s]['excluded_stale_after_return'],data[s]['max_queries_per_episode']) for s in ('train','val','test')])
 p('episode 键为 `(session, app, last_departure, observation_end)`。相同时间戳保持原始事件顺序；候选必须处于离开之后、下一次返回之前。每个 episode 的所有 query 权重和为 1，包括 mask 全零的 query；后者 loss=0，不伪造标签。one-query-per-episode 每 epoch 在该 episode 的全部 query 中均匀随机取一条。两种模式各训练一份模型。');p()
 p(f"跨划分的物理 episode 数：{data['cross_partition_physical_episodes']}。这些 episode 在分区边界做行政删失，分区内分别加权，训练标签不跨验证/测试边界。原历史数据划分已在此前实验中使用，明确视为开发评估，不重新命名为未触碰 holdout。");p()
 p('## 2. M2 与独立优先级层');p()
 p('LSTM 与显式特征分支保持既有结构；仅终端输出变为 5 个 survival logits。`z0` 自由，后续 `zk=z0−Σsoftplus(dj)`，因此 P(T>t) 随 t 单调不增。阈值为 3/10/30/60/180 秒。使用 masked BCE，每个 query 先对已知阈值求均值，再应用 episode 权重。T=47 标签为 [1,1,1,0,0]；删失下界75秒的 mask 为 [1,1,1,1,0]。');p()
 p('risk30=1−S(30)，risk180=1−S(180)。coldness 为 [0,180] 上 survival 曲线的梯形积分除以180（截断平均存活时间）；不声称预测180秒以后的实际平均返回时间。uncertainty 是 5 个 Bernoulli 概率的平均归一化熵，仅诊断，不参与 mapper 或门禁。');p()
 p('RankOnly 按 coldness 的相对秩映射到 Bin0–Bin7；Bin0 热、Bin7 冷。RiskAwareRank 用预先固定的 risk30≥0.5 或 risk180≥0.8 将高风险候选放到 Bin0–Bin1，其余按 coldness 放到 Bin2–Bin7。模型不返回 bin，mapper 不读取训练时间类别。不同预测阈值数量仍使用相同 8-bin mapper，单元测试已验证。相同分数同 bin；回收选择在 bin 并列时按 app_id 固定打破并列，POA 并列计0.5。');p()
 p('## 3. 训练与选择');p()
 rows=[]
 for mode in CFG['sampling_modes']:
  hist=json.loads((OUT/mode/'history.json').read_text());best=min(hist,key=lambda r:r['val_episode_bce']);steps=sum((r['queries']+CFG['batch_size']-1)//CFG['batch_size'] for r in hist);rows.append((mode,len(hist),steps,best['epoch'],best['val_episode_bce'],hist[-1]['elapsed_s']))
 table(['采样','实际 epochs','优化步数','最佳 epoch','验证 episode BCE','训练秒'],rows)
 p('M2 从头训练，复用 encoder 架构而非冻结或迁移已训练权重。两模型同 seed=42、Adam lr=0.001、batch=2048、CPU 单线程；weighted 最多20 epoch、至少6 epoch、4轮无改善早停；one-query 最多80 epoch、至少12 epoch、8轮无改善早停。one-query 每轮优化步数约小14倍，因此在测试评估前根据验证曲线扩大其预算，并从相同seed重跑。两模式不是等优化步数对照，实际步数如表。以验证 BCE 选择 checkpoint，未使用测试指标调参。单 seed 的优化方差未被用户 bootstrap 覆盖。当前 p180/M1 使用已有 checkpoint，本轮比较回答最终方案是否更好，不将收益全部归因于 ordinal head。');p()
 p('## 4. LSApp 离线主指标');p()
 p('所有 B0–B4 使用完全相同的 query、后台候选和删失边界。Episode POA 先在同一 episode-pair 内平均重复 query 的排序正确率，再对唯一 episode-pair 求平均；分数并列0.5，真实时间并列不比较，两个无法证明次序的删失候选不比较。其他 headline 指标每个 episode 只取第一个可用决策点，多 episode 同一决策仍按 focal episode 计，每个 episode 一票。主表限至少2候选；query-row POA 仅辅助。');p()
 p('Cold Precision@K 要求真实 Top-K 可确定且候选数>K；DVR 使用 T≤30/T≤180，可判定才计；删失下界不足不当成安全。Victim/EVR 仅在返回时间或最早返回可识别时统计；victim median 是已观察返回的条件中位数，需结合分母看待，不能把删失当无穷大。Gate 比较仅用两方法共同已知结果。');p()
 metrics=[('episode_poa','Episode POA'),('cp1','Cold Precision@1'),('cp3','Cold Precision@3'),('dvr30','DVR@30 ↓'),('dvr180','DVR@180 ↓'),('victim_time','Victim median s ↑'),('evr2','EVR@2 median s ↑'),('evr4','EVR@4 median s ↑'),('first_anchor_poa','First-anchor POA')]
 for mode,reports in off.items():
  p(f'### {mode}');p();names=list(reports);rows=[]
  for k,label in metrics:
   vals=[]
   for name in names:
    r=reports[name];x=r[k];vals.append(f"{fmt(x['value'])} (n={x['n']})" if isinstance(x,dict) else fmt(x))
   rows.append([label]+vals)
  table(['指标']+names,rows)
  p('辅助 query-row 诊断（不作为 headline 或门禁依据）：');p()
  table(['方法','Query-row POA','唯一 episode pairs','episode decisions','bin/score 并列比例'],[(name,reports[name]['query_row_poa'],reports[name]['unique_episode_pairs'],reports[name]['episode_decisions'],reports[name]['bin_or_score_tie_fraction']) for name in names])
 p('B3/B4 主表使用实际 bin 优先级，不能拿未量化的连续 coldness 冒充内核最终优先级；B0–B2 为现有基线排序。因此 M2 的量化并列损失也包含在比较中。');p()

 p('Mapper 诊断（同一候选集合）：');p();diag=[]
 eligible=np.load(OUT/'dataset/test/eligible.npy',mmap_mode='r');multi=eligible.sum(1)>=2
 for mode in CFG['sampling_modes']:
  with np.load(OUT/mode/'risk-priority-test.npz') as z:
   continuous=np.where(eligible,z['coldness'],-np.inf).argmax(1)
   for key in ('rank_bins','risk_bins'):
    bins=z[key];victims=np.where(eligible,bins,-100).argmax(1);occupancy=np.bincount(bins[eligible],minlength=8).tolist();changed=float(np.mean(victims[multi]!=continuous[multi]));diag.append((mode,key,occupancy,changed))
 table(['采样','mapper','Bin0..7候选行占用（仅诊断）','相对连续coldness改变victim比例'],diag)
 p('失败诊断：两种采样下 M2 的 Episode POA 均低于 p180，DVR@30/180 均更高；one-query 的结果较 weighted 好，但仍未达到基线。RankOnly 在本候选集合中没有产生排序并列或改变连续 coldness 的 victim，因此其性能下降不能归因于 8-bin 量化。RiskAwareRank 增加少量并列，未改善结果。当前证据指向本版预测与 coldness 排序方案不足，无法仅据此区分训练目标、时间尺度、特征或优化各自的贡献；按 FAIL→STOP 要求，本轮不再调参重训。');p()
 p('## 5. A11y-CUA 外部验证');p()
 p('仅 SU1–SU8 共480 session；所有模型冻结，A11y 不训练、不选择 checkpoint、不调 mapper。外部验证使用连续 risk/coldness 与 3/10/30/60/180 秒，不强行映射8-bin。');p()
 for kind,result in a.items():
  p(f'### {kind}');p();au=result['audit'];table(['项目','数值'],[(k,v) for k,v in au.items() if isinstance(v,(int,float))])
  inv={v:k for k,v in VOCAB.items()};table(['真实身份','预测槽位 ID','预测槽位 App'],[(name,slot,inv[slot]) for name,slot in au['identity_to_proxy'].items()])
  for mode,rr in result['models'].items():
   p(mode);p();r=rr['ranking'];table(['方法','Episode POA','CP@1','DVR@30','DVR@180','Victim median s'],[(name,x['episode_poa'],x['cp1']['value'],x['dvr30']['value'],x['dvr180']['value'],x['victim_time']['value']) for name,x in r.items()]);table(['阈值秒','可判定候选行','episode-weighted Brier'],[(t,x['known_candidate_rows'],x['episode_weighted_brier']) for t,x in rr['ordinal_calibration'].items()])
 p('**Process-level 是外部验证的优先口径。** 本报告的 process-level 指 executable/application 字段，不是 PID 生命周期。原生进程身份从 window.application 提取并保留；Word/Excel/PowerPoint 等不合并成一次 App 停留。冻结的30-App模型需要明确的 process→词表槽位适配，因此不同进程可能共用同一预测槽位，候选与真值仍保持分开，碰撞数如上；无法唯一映射的 ApplicationFrameHost 和无对应项保留为观察屏障。完整原生 process reentry 另存 CSV，覆盖所有进程，不受预测槽位限制。这里是有损 proxy transfer，不是经过原生进程词表训练的预测器；不能将其结果当作已解决真实 PC identity 泛化。');p()
 p('## 6. Gate-2');p()
 p('外部验证诊断：process 口径下 M2 的 POA 高于迁移后的 p180，但仍低于 Recency；functional 口径下则低于 p180。原生已观察 process episodes 为986，适配模型可评估701（71.1%），且存在共享预测槽位的碰撞。这些结果不支持稳定的跨域优势，不能推翻 LSApp Gate 的失败。');p()
 p('门禁在训练前固定：按用户成组的配对 bootstrap 2000 次；POA 差值95%CI下界>0；DVR@30/180 点估计不增加且CI上界≤0（零非劣界）；CP@1、CP@3或 victim median 至少一项CI下界>0；first-anchor POA 也必须CI下界>0。最后要求同一 mapper 在两种采样训练下都通过，避免依赖 periodic query 权重。');p()
 for mode,gg in g['sampling_gates'].items():
  for mapper,r in gg.items():
   p(f"### {mode} / {mapper}：{r['status']}");p();table(['条件','通过'],list(r['conditions'].items()));table(['指标','共同支持 n','用户数','p180','M2','差值','95% CI'],[(k,x['n'],x.get('users',0),x.get('baseline'),x.get('m2'),x.get('delta'),str(x['ci95'])) for k,x in r['checks'].items()])
 p(f"**最终 Gate-2：{g['status']}。通过 mapper：{g['passing_mappers']}。STOP。**");p()
 p('## 7. 产物、验证与复现');p()
 p(f"数据与 checkpoint 独立验证：{verification['status']}。检查每个 episode 权重和=1、返回端点一致、观察/删失 episode 不混合、单调 survival、risk30≤risk180、checkpoint 重放一致。单元测试覆盖标签边界、删失 mask 梯度、权重重复不变性、每 episode 抽样、mapper 风险保护和阈值数/bin数独立。");p()
 p('```bash\ncd /home/lzx/Desktop/PARP/lzx-zr/experiments/reentry_priority_v2\npython3 build_dataset.py\npython3 train.py episode_weighted\npython3 train.py one_query_per_episode\npython3 evaluate.py\npython3 a11y.py\npython3 verify.py\npython3 gate.py\npython3 report.py\n```');p()
 guard=json.loads((OUT/'runtime-kernel-guard.json').read_text());p(f"Runtime/kernel 保护检查：评估前后既有 git diff 哈希一致={guard['unchanged']}；没有覆盖用户原有修改。");p()
 p('源码：`model.py`、`priority.py`、`build_dataset.py`、`train.py`、`evaluate.py`、`a11y.py`。机器可读：`outputs/dataset/audit.json`、各模式 `history.json`/`checkpoint.pt`/`risk-priority-test.npz`/`episode-metric-support.npz`、`outputs/a11y/validation.json`、`outputs/gate2.json`、`outputs/verification.json`。没有启动 Native MGLRU/p180-bin/M1-bin/M2 内核对照，也没有声称测得 reclaimed bytes、refault、fault、swap-in、PSI、reentry latency 等内核收益。')
 path=ROOT.parents[2]/'FINAL-OFFLINE-REPORT.md';path.write_text('\n'.join(lines)+'\n');print(path)
if __name__=='__main__':main()
