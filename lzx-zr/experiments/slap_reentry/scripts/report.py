#!/usr/bin/env python3
import argparse,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dataset.build import write


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();cfg=json.loads(a.config.read_text());out=Path(cfg['output'])
    evaluation=json.loads((out/'metrics/offline.json').read_text());g=evaluation['gate1'];diag=json.loads((out/'metrics/diagnosis.json').read_text())
    b=evaluation['scopes']['multi_candidate']['p180'];m=evaluation['scopes']['multi_candidate']['SLAP-style']
    frozen=json.loads((out/'baseline/manifest.json').read_text());integrity={name:hashlib.sha256(Path(name).read_bytes()).hexdigest()==h for name,h in frozen['source_hashes'].items()}
    kernel_hashes=json.loads((out/'baseline/additional-kernel-source-hashes.json').read_text())
    integrity.update({name:hashlib.sha256((ROOT.parents[2]/name).read_bytes()).hexdigest()==h for name,h in kernel_hashes.items()})
    traincfg=json.loads((out/'metrics/training-config.json').read_text());integrity.update({name:hashlib.sha256(Path(name).read_bytes()).hexdigest()==h for name,h in traincfg['source_hashes'].items()});assert all(integrity.values())
    write(out/'metrics/source-integrity.json',integrity)
    import matplotlib
    matplotlib.use('Agg');import matplotlib.pyplot as plt
    history=json.loads((out/'metrics/training-history.json').read_text());fig,ax=plt.subplots()
    for k in ['train_ce','val_ce']:ax.plot([r['epoch'] for r in history],[r[k] for r in history],label=k)
    ax.set(xlabel='Epoch',ylabel='Masked candidate CE');ax.legend();fig.tight_layout();fig.savefig(out/'figures/training_curve.png');plt.close(fig)
    lines=['# FINAL REPORT','',f'离线实现、验证和三方法评估完成。Gate-1: **{g["status"]}**。','',
        '| 阶段 | 状态 | 证据 |','|---|---|---|','| 代码审计 | PASS | CODE-AUDIT.md |','| M0冻结 | PASS | BASELINE.md / baseline/manifest.json |',
        '| 数据及标签验证 | PASS | DATASET-VALIDATION.md / dataset/SOURCE-REVIEW.md |','| M1训练 | PASS | TRAINING-REPORT.md / checkpoints/M1.pt |',
        '| 离线三方法评估 | PASS | OFFLINE-EVALUATION.md / metrics/offline.json |',f'| Gate-1 | {g["status"]} | metrics/gate1.json |','',
        '## Q1：是否改善后台应用排序？',f'多候选POA：p180={b["poa"]:.4f}，M1={m["poa"]:.4f}；相对变化 {g["checks"]["poa"]["relative_improvement"]:.2%}。Cold Precision@1/2/3及有效样本数见离线报告。完整原M0查询另列，避免把新增周期query混入旧结果。','',
        '## Q2：是否更少选择即将返回的应用？',f'DVR@30：p180={b["dvr30"]["mean"]:.4f}，M1={m["dvr30"]["mean"]:.4f}；DVR@180：p180={b["dvr180"]["mean"]:.4f}，M1={m["dvr180"]["mean"]:.4f}。须同时查看common-known配对比较与置信区间，不能用未知标签制造安全率。','']
    if g['status']=='FAIL':
        lines+=['## Q3–Q5：回收归属、系统收益与开销收益比','未验证。按用户Gate-1要求，未执行runtime接入或系统压力实验，未向/dev/myfs/debugfs写入新预测，未修改内核/MGLRU/回收量/常驻服务。离线结果不能回答系统收益和真实PC延迟。','',
            'Gate未通过后已进行标签/特征一致性、query分布、类别占比、历史覆盖和同segment排序诊断，见DIAGNOSIS.md。没有放宽门槛，也没有加入Transformer/survival/ranking loss。']
    else:lines+=['## Q3–Q5','待Gate后的runtime与系统实验完成后更新，尚不能宣称系统收益。']
    checks=g['checks']
    lines+=['','## 相同可判定query的配对比较',
        '| 指标 | n | p180 | M1 | M1−p180的用户成组95%区间 |',
        '|---|---:|---:|---:|---|']
    for key in ['dvr30','dvr180']:
        c=checks[key]
        lines.append(f'| {key} | {c["n"]:,} | {c["p180"]:.3%} | {c["slap"]:.3%} | [{100*c["ci95"][0]:+.3f}, {100*c["ci95"][1]:+.3f}] 个百分点 |')
    lines+=['','## 本轮结论与诊断',
        '分段目标改善了排序指标的点估计，但没有证明“更安全地选择回收对象”。相比p180，30秒和180秒危险victim比例都上升；在共同可判定的query上做用户成组配对bootstrap，两个上升的95%区间也均大于0。',
        'POA差值的用户成组95%区间为[-1.58, +8.97]个百分点，尚不能确认跨用户的稳定改善。验收中的其他指标没有达到明确改善，所以Gate-1失败并非仅由5%的参考门槛决定。',
        f'20轮训练已完成，按验证CE选择第{diag["best_epoch_by_validation_ce"]["epoch"]}轮checkpoint；训练CE由1.698降到1.150，而验证CE由1.725升到2.579，存在明显过拟合。',
        '预测argmax在全部246,009个候选样本中有95.73%落在C5或C7；这描述分类分布，不能据此断言完整概率或冷度分数无法区分应用。类别不平衡的因果影响尚需独立消融。',
        '切换query的POA仅从64.89%变为65.07%，周期query从61.57%变为67.34%。增益随query类型明显不同，不能将新采样分布下的总体增益全部归因于预测head。',
        '标签检查与M0输入/预测一致性检查均通过，未发现能解释失败的构造错误。20段历史中缺失last-enter/leave记录的候选占2.78%；LSApp的opened近似及手机到PC的行为差异仍需真实焦点数据验证。',
        '后续合理方向是预先定义query采样、正则化和类别分布消融，并采用新的时间留出；本轮没有据测试集继续调参。',
        '', '## 检查与开销边界',
        '10项单元测试通过；独立核对300个源事件样本，并重现原M0全部106,642个测试query预测。新checkpoint的128条离线重放最大绝对误差为1.79e-7。',
        'CPU单query计时保存在metrics/checkpoint-verification.json；主机未隔离后台负载，尾延迟波动较大，只作离线诊断，不能作为在线开销或系统收益验收。参数量77,656，参数张量310,624字节不等于进程RSS。',
        '最终环境取证见metrics/final-environment.json。本任务没有执行服务重启命令；日志显示常驻服务在12:27:47由systemd自动重启了一次，退出原因尚未确定，因此不声称服务PID全程不变。bin-reclaim开关仍为0。',
        '', '## 关键限制','- LSApp opened state != true PC resident process state；手机→PC映射不是行为等价证明。',
        '- 新增30秒query改变训练/评估采样分布，保持旧时间边界不等于保持原样本量；本轮同时报告原query子集，但尚不能把差异仅归因于head。',
        '- CDF/重入时间统计基于已观察返回；未知样本没有填成无穷大。',
        f'- 验证CE最佳为第{diag["best_epoch_by_validation_ce"]["epoch"]}轮；训练/验证曲线见training_curve.png。',
        '- 单seed，既有测试集已被多次诊断；最终泛化需要新留出或真实焦点数据。','',
        '## 交付','checkpoint: checkpoints/M1.pt；完整8概率: predictions/M1-test-probabilities.npy 和 predictions/predictions.csv；排序明细: predictions/ranking_results.csv；分类矩阵/分段分布/CDF/训练曲线: figures/；复现命令见实验README。']
    (out/'FINAL-REPORT.md').write_text('\n'.join(lines)+'\n')
    files=[f for f in out.rglob('*') if f.is_file() and f.name not in ['progress.json','artifact-hashes.json'] and (f.suffix in ['.md','.json','.pt','.png'] or f.name in ['CONFIG.yaml']) and 'baseline' not in f.parts]
    write(out/'metrics/artifact-hashes.json',{str(f.relative_to(out)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    write(out/'progress.json',{'phase':'offline complete','gate1':g['status'],'runtime':'NOT RUN' if g['status']=='FAIL' else 'PENDING','final_report':str(out/'FINAL-REPORT.md')})
    print('Final report written; Gate-1',g['status'])


if __name__=='__main__':main()
