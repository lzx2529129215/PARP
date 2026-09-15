from common import *
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    read=lambda n:json.loads((OUT/'metrics'/n).read_text())
    switch_effect=read('switch_training_effect.json')
    uc=read('user_coverage.json');prior=read('app_class_prior.json');fc=read('feature_collisions.json');e=read('effective_samples.json');c=read('class_collapse.json')['all'];h=read('prediction_entropy.json');s=read('sampling_weight_audit.json');b=read('simple_baselines.json');eb=read('episode_balanced_baselines.json');sw=read('switch-evaluation.json');o=read('original-evaluation.json');m=read('mapping_distortion.json');one=read('one_to_one_comparison.json');raw=read('original_effective_samples.json')
    for f in ['EFFECTIVE-SAMPLE-AUDIT.md','CLASS-DISTRIBUTION-DIAGNOSIS.md','SWITCH-ONLY-ABLATION.md','SIMPLE-BASELINES.md','ORIGINAL-VS-MAPPED.md','MAPPING-DISTORTION.md','HOLDOUT-MANIFEST.json']:assert (OUT/f).exists(),f
    fig,axes=plt.subplots(1,2,figsize=(10,4))
    for ax,v in zip(axes,['switch','original']):
        hist=read(v+'-training-history.json');assert len(hist)==20
        for key in ['train_ce','val_ce']:ax.plot([r['epoch'] for r in hist],[r[key] for r in hist],label=key)
        ax.set(title=v,xlabel='Epoch',ylabel='CE');ax.legend()
    fig.tight_layout();fig.savefig(OUT/'figures/ablation_training_curves.png');plt.close(fig)
    t=e['test/all'];tb=e['test/periodic'];tr=e['train/all'];r=o['reports']['shared_multi'];a=r['M1-original'];z=r['M1-mapped-mixed'];sg=sw['reports']['shared_multi'];balanced=s['all']['episode_pair_balanced_poa']
    lines=['# ROOT-CAUSE REPORT','','Root-Cause Pack completed through offline diagnosis. STOP: no new objective/model, real-PC collection, runtime or kernel integration is started. All historical test results below are development evidence, not a new untouched final evaluation.','',
        '本轮根因判断：最强实证来自SAMPLING（重复采样与评价权重）和DATA（有限且不均匀的应用重入覆盖），同时确认当前排序评分与短期安全目标存在取舍。未发现足以把30-App映射认定为主要性能瓶颈的证据；也未证明flat CE或LSTM容量是唯一根因。无需凭本轮结果自动换模型或接内核。','',
        '指标口径：POA是在真实先后顺序可判定的候选对上计算排序正确率，预测并列不计正确；Cold Precision@K是预测最晚返回的K个应用与真实最晚K个应用的集合重合率，仅使用集合可判定且候选数>K的查询。DVR@30/180是优先回收对象在严格小于30/180秒内返回的比例，越低越好；观察不足的未知结果排除。victim_time仅对已观察到返回的被选对象报告秒数中位数，不是含删失数据的生存中位数。表中n是相应指标的有效分母；两方法DVR的严格配对比较另取共同可判定查询。置信区间按用户成组bootstrap，未覆盖不同训练随机种子的变异。','',
        '## Q1：246k候选有多少独立重入episode？',
        f'测试集共{t["candidate_rows"]:,}条候选，其中{t["observed_rows"]:,}条有观察到返回，去重为{t["unique_reentry_episodes"]:,}个episode；每个平均{t["observed_rows_per_episode"]:.2f}条。另有{t["censored_rows"]:,}条删失记录、{t["censored_groups_not_observed_episodes"]:,}个删失组，不能把它们伪装成已知重入。训练集有{tr["unique_reentry_episodes"]:,}个观察到的episode，平均重复{tr["observed_rows_per_episode"]:.2f}次。去重episode仍共享用户及历史，不等于IID独立样本。','',
        f'应用覆盖不均：训练候选中，GIMP仅{e["train/all"]["per_app"]["GIMP"]["unique_reentries"]}个、LibreOffice仅{e["train/all"]["per_app"]["LibreOffice"]["unique_reentries"]}个观察到的重入episode；二者在测试候选中均为0。这里指符合后台候选定义且观察到返回的episode，不是源文件所有启动事件数。不能用总体POA替这些应用的真实PC返回预测作保证。',
        '## Q2：periodic是否放大数据和POA？',
        f'是，明确放大行数：测试periodic有{tb["candidate_rows"]:,}条候选，但仅{tb["unique_reentry_episodes"]:,}个观察到的episode，观察到返回的记录平均重复{tb["observed_rows_per_episode"]:.2f}次。switch和periodic episode集合重叠，不可相加。',
        f'查询加权POA为p180 {s["all"]["row_weighted_poa"][0]:.2%} → M1 {s["all"]["row_weighted_poa"][1]:.2%}；按{ s["all"]["unique_episode_pairs"]:,}个可判定episode对等权后，变为{balanced[0]:.2%} → {balanced[1]:.2%}。M1−p180的用户成组95%CI为{s["all"]["paired_user_bootstrap"]["ci95"]}。排序增益依赖评估权重，不构成独立episode上稳定提升的证据。',
        '周期采样不必然是错误：若回收决策按实际后台停留时间发生，按时间加权有其用途。问题是不能把重复行当成新增独立行为，或把时间加权增益泛化成所有重入事件的增益。','', 'Switch-only重训（仅训练query过滤；验证集保持不变）：','',table(sg),
        'Switch-only相对混合分段模型的配对结果另见metrics/switch_training_effect.json：直接比较同一objective下的训练采样效果。该对照保持20轮，但去掉periodic也减少每轮样本与SGD更新数，不能把全部变化单独称为分布污染。验证选择仍沿用完整原验证集，并非另调switch验证策略。',
        f'去掉periodic后，switch查询POA从{sg["M1-mapped-mixed"]["poa"]:.2%}升至{sg["M1-switch"]["poa"]:.2%}，但差值CI跨0；共同可判定查询上的DVR30从{switch_effect["switch_minus_mixed_paired"]["dvr30"]["rates"][0]:.2%}降至{switch_effect["switch_minus_mixed_paired"]["dvr30"]["rates"][1]:.2%}，差值CI={switch_effect["switch_minus_mixed_paired"]["dvr30"]["ci95"]}。采样变化改善了混合模型的短期风险，但S与p180的排序差异尚不明确，且S的DVR30仍更高。',
        '该对照检验训练采样变化；同一批模型按不同权重评估检验评价权重变化。两者不能混为同一因果结论。详细置信区间见SWITCH-ONLY-ABLATION及metrics/switch-evaluation.json。','',
        '## Q3：真实标签是否天然集中在C5/C7？',
        f'同一批{c["n_matched_valid"]:,}个有效候选上，真实C5+C7={sum(c["true_fraction"][k] for k in [5,7]):.2%}，argmax={sum(c["argmax_fraction"][k] for k in [5,7]):.2%}，平均预测概率质量={sum(c["mean_probability"][k] for k in [5,7]):.2%}。R57(argmax)={c["R57_argmax"]:.3f}，R57(概率质量)={c["R57_mean_mass"]:.3f}。存在明显argmax集中，但不是整个概率质量都塌缩到远期类别；总体边际概率接近也不代表条件概率已校准。',
        f'危险<30s victim的归一化熵中位数={h["dangerous_lt30"]["normalized_entropy"]["median"]:.3f}，已知安全≥30s为{h["known_safe_ge30"]["normalized_entropy"]["median"]:.3f}。危险组max(p)≥0.8比例={h["dangerous_lt30"]["fraction_max_p_ge_0.8"]:.2%}。多数危险错误伴随较高分段不确定性，不能概括为高置信度错误；熵本身也不是回收决策的校准置信度。','',
        f'完整输入相同但分段标签不同的候选记录占{fc["fraction_rows_in_conflicting_groups"]:.2%}（{fc["rows_in_conflicting_groups"]:,}条），见metrics/feature_collisions.json。episode重复不等于输入完全重复；不能把重采样倍数直接当作同样倍数的无效训练数据。',
        '## Q4：LSTM是否明显优于简单时间基线？','',table({k:b['reports']['all_multi'][k] for k in ['AppMedian','UserAppMedian','LastReentry','EMA','M0-p180','M1-mixed']}),
        '额外的训练集AppClassPrior计数基线（与M1使用相同分段频率目标）：',table(prior['reports']['all_multi']),
        'M1相对AppClassPrior的用户成组POA差值区间：'+str(prior['M1_minus_prior_paired']['poa']['ci95'])+'。不能只凭比interval-median高就断言序列贡献已被充分隔离。',
        'episode对等权POA：'+json.dumps(eb['poa'],ensure_ascii=False)+'。各方法与M1的用户成组CI在metrics/episode_balanced_baselines.json。',
        '这些对照表明输入历史包含可用信号；是否“明显更好”须同时看配对CI、采样权重及DVR，不能只看一个总体POA。简单interval启发式与扣除已过后台时间的remaining版本均保留；后者的0分并列不计正确，不能把其低POA当成低于随机的直接证据。','',
        '具体判断：M1优于Last和AppMedian；但EMA与AppClassPrior已提供较强基线。episode对等权时，M1约66.66%，EMA约64.43%、AppClassPrior约63.86%，差值的用户bootstrap区间均为正，说明并非完全没有额外排序信号；同时M1仍低于p180约69.67%，且整体DVR30更差。不能据此宣称当前LSTM已实现可靠的回收收益，也不能宣称所有方法都接近随机。','',
        '## Q5：30-App映射是否显著破坏可预测性？',
        f'87个原始应用中72个进入30个真实目标，15个进入UNKNOWN。原始切换保留率={m["switch_retention"]:.2%}，消失{m["collapsed_switches"]:,}次；原始A→B→A中有{m["collapsed_ABA_triples"]:,}/{m["raw_ABA_triples"]:,}组完全合并。信息损失存在，但不能据此直接判为主要瓶颈。','',table(r),
        f'原始模型best epoch={o["best_epoch"]}，validation CE={o["best_validation_ce"]:.6f}。原始测试去重观察episode={raw["unique_reentry_episodes"]:,}。',
        '完整空间对照共用时间戳、用户、采样策略、20段历史和训练参数，但词表导致输入/输出矩阵大小、候选集合、标签及opened近似变化。另做one-to-one相同候选、相同返回标签验证：','',table(one['reports']),
        f'结论：本轮没有支持“30-App映射显著破坏预测、且是主要瓶颈”的证据。完整空间原始POA={a["poa"]:.2%}，映射={z["poa"]:.2%}；原始−映射的用户CI={o["paired_comparison"]["shared_multi"]["poa"]["ci95"]}。一对一子集{one["queries"]:,}个查询、{one["reports"]["mapped"]["comparable_pairs"]:,}个可判定对上，原始={one["reports"]["original"]["poa"]:.2%}、映射={one["reports"]["mapped"]["poa"]:.2%}，CI={one["paired"]["poa"]["ci95"]}，未显示明确排序优势。该子集原始DVR30={one["reports"]["original"]["dvr30"]["mean"]:.2%}，映射={one["reports"]["mapped"]["dvr30"]["mean"]:.2%}，也未显示短期安全收益。没有显著差异不等于证明两个空间等价；尤其本轮只有一个训练seed、两种词表参数量不同，一对一子集只涉及部分应用。详见ORIGINAL-VS-MAPPED.md。','',
        '## Q6：根因归类 / Gate-R',
        '- SAMPLING：证据最明确的是重复采样及评价权重敏感；原有正向POA增益在episode对等权后反转。训练采样影响另由M1-S对照衡量。',
        '- DATA：观察到的episode数量远小于候选行数；删失及opened状态近似限制了有效监督。它们不是可以凭增加epoch消除的因素。',
        f'用户覆盖：全部测试查询（包括零候选）涉及{uc["users_by_split"]["test"]}个用户，其中{uc["test_users_unseen_in_training"]}个未出现在训练阶段；{uc["test_candidate_rows_unseen_users"]/uc["test_candidate_rows_total"]:.2%}的候选行、{uc["test_multi_queries_unseen_users"]/uc["test_multi_queries_total"]:.2%}的多候选查询来自训练未见用户。模型输入只有通用user_group，没有用户ID；这包含跨用户泛化，不等同于同一PC用户的长期个性化预测。该统计不单独证明性能差异由新用户造成。',
        '- LABEL：源事件与标签核对通过；类别边际不均衡，但C5/C7真实占比远低于argmax占比，不能解释成“几乎所有真实标签都远期”。',
        '- MAPPING：确有身份合并、切换消失和UNKNOWN覆盖损失，但原始空间并未明显改善排序或短期安全，不支持把映射定为主要瓶颈。此结论也不表示手机事件映射已能代表真实PC使用行为。',
        '- OBJECTIVE：平均分段编号允许远期概率补偿近期风险；总体排序和top-victim安全目标不等价。本轮没有训练ordinal或ranking对照，不能宣称已经证明flat CE是唯一根因。',
        '机制示例（不是本轮观测样本）：A的P(C0)=0.25、P(C7)=0.75，score=5.25；B的P(C5)=1，score=5。即使概率完全准确，当前评分仍选A，而其30秒内返回风险为25%、B为0%。因此需区分概率估计误差与评分目标的风险取舍；仅把head改成ordinal不保证同时改善DVR。',
        '- MODEL：历史信号存在；原M1后续epoch验证损失恶化，但已采用验证最优checkpoint，不能把最终失败单独归因于训练轮数过多。没有证据表明仅需增加LSTM容量或更换Transformer。','',
        '## 留出集与完成边界',
        '旧test永久作为development。已按session起点冻结60/15/15/10历史分区，跨边界session隔离，不计算该新分区的模型指标；但最后10%历史已被过去实验使用，仍不是真正未见holdout。HOLDOUT-MANIFEST明确标注真正final holdout等待新未见数据，不能把重新切分当作消除泄漏。',
        '本轮到此STOP。未自动开展4-class、ordinal、Transformer、survival、ranking loss、真实PC采集、runtime或kernel实验。报告中的后续方向是待决策事项。','',
        '## 验证与复现',
        '原始空间按同一时间戳对齐时，保留同时间戳事件的源顺序；已验证映射压缩后的segment序列与冻结mapped源逐项一致。构建过程的首次同时间戳歧义与NumPy JSON序列化错误已修复并从独立新目录重跑，错误日志保留。原始数据各分区源扫描样本与校验状态见dataset/original/validation.json。',
        '执行优化单独记录：先将稀疏查询的实际特征装入内存，首轮损失与原运行完全一致；后经同批次检查，将CPU执行线程2→1，logits最大差异5.96e-7，避免当前双vCPU主机的严重线程开销。未改变学习超参数或模型结构；冻结mapped M1来自此前双线程运行，数值执行差异已披露。旧中间记录保留。',
        '单元测试、全概率与逐query排序、逐轮训练曲线、配置和源hash均随包保存。模型选择只看旧验证集，未用旧test选择epoch/边界。当前主机存在其他后台任务，训练墙钟耗时不作为模型开销指标。']
    (OUT/'ROOT-CAUSE-REPORT.md').write_text('\n\n'.join(lines)+'\n')
    sources=[p for p in (ROOT/'root_cause').glob('*.py')]+list((ROOT/'models').glob('*.py'));write(OUT/'metrics/final-source-hashes.json',{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    write(OUT/'progress.json',{'status':'COMPLETE','phase':'Root-Cause Pack STOP','models':'switch and original trained 20 epochs; mapped M1 frozen reused','runtime':'NOT RUN','kernel':'NOT RUN','genuinely_unseen_holdout':'PENDING NEW DATA','report':str(OUT/'ROOT-CAUSE-REPORT.md')});print('Root-Cause Pack COMPLETE; STOP',flush=True)
if __name__=='__main__':main()
