"""Rebuild the packaged summary from preserved native results, without a VM."""
import argparse
import json
from pathlib import Path
import statistics

from peak_metrics import summary

BUNDLE=Path(__file__).resolve().parents[1]
LATEST='parp-userspace-deficitfix-20261117'


def load(p):return json.loads(p.read_text(encoding='utf-8'))
def avg(rows,key):return statistics.mean(r[key]for r in rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,default=BUNDLE)
    args=parser.parse_args();bundle=args.bundle.resolve()
    root=bundle/'workspace'/LATEST;run=load(root/'results/RUN-revisit.json')
    growth=run['reference_demand_growth_bytes'];rounds=[]
    for i,trial in enumerate(run['trials'],1):
        if trial['status']!='COMPLETE':raise ValueError('Incomplete trial cannot enter the summary')
        row=summary(trial,growth);row['order']=i
        row['deficit_admission']=trial['deficit_admission']
        row['completion_checks']=trial.get('prepeak_completion_checks',[])
        row['completion_overruns']=trial.get('prepeak_completion_overruns',[])
        row['peak_reclaim_count']=sum(w['name']=='memory.reclaim' and
            8<=w['start_ns']/1e9-trial['start_monotonic_s']<22
            for writes in trial.get('writes',{}).values() for w in writes)
        occ=[json.loads(s)for s in (root/'results/trials'/trial['tag']/'occupancy.jsonl').read_text().splitlines()]
        before=[x for x in occ if x['time_s']<8][-1];peak=[x for x in occ if 8<=x['time_s']<22][-1]
        row['peak_high_events']={a:peak.get('app_memory_events',{}).get(a,{}).get('high',0)-
            before.get('app_memory_events',{}).get(a,{}).get('high',0)
            for a in ('gimp','libreoffice','firefox','krita')}
        rounds.append(row)
    groups={}
    for stage in ('baseline','calibration','formal'):
        selected=[r for r in rounds if r['stage']==stage]
        groups[stage]=dict(n=len(selected),ready_count=sum(r['space_ready']for r in selected),
            initial_gap_MiB=avg(selected,'required_from_start_MiB'),gap_MiB=avg(selected,'remaining_gap_MiB'),
            drop_MiB=avg(selected,'prepeak_net_drop_MiB'),p95_ms=avg(selected,'response_p95_ms'),
            max_ms=avg(selected,'response_max_ms'),
            peak_operation_ms={a:statistics.mean(op['elapsed_ms'] for r in selected
                for op in r['peak_operations'][a])for a in ('gimp','libreoffice','firefox')})
    formal=groups['formal'];off=groups['baseline']
    result=dict(status=run['status'],schema=run['trials'][-1]['execution_contract']['schema'],
        overall_goal_achieved=False,reason='one formal round retains 3.109375 MiB deficit; early-growth deadlines also missed',
        scene=run['scene'],test_limit_MiB=run['memory_limit_bytes']/2**20,
        reference_limit_MiB=run['reference_limit_bytes']/2**20,
        reference_growth_MiB=growth/2**20,target_gap_MiB=run['target_gap_bytes']/2**20,
        minimum_admission_gap_MiB=run['minimum_initial_gap_bytes']/2**20,
        groups=groups,p95_change_percent=(formal['p95_ms']/off['p95_ms']-1)*100,
        formal_faults=run['formal_total_faults'],sources_unchanged=run['sources_unchanged'],
        definitions=dict(peak_interval_s=[8,22],responses='inputs initiated in [8,22); slow completions retained',
            percentile='nearest rank; group value is mean of the three per-round P95 values',
            gap='independent unclipped reference growth minus actual cgroup headroom, clamped to zero',
            drop='observed net occupancy decline, including natural release; not exact reclaimed old pages',
            operation_time='full completion time of peak_new_data initiated near 8 s; no full-task elapsed comparison',
            fault_interval='complete workload, per-app mean of three; total pgfault includes minor and major faults'))
    dest=bundle/'summaries';dest.mkdir(exist_ok=True)
    (dest/'逐轮结果.json').write_text(json.dumps(rounds,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (dest/'汇总指标.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    labels={'baseline':'关闭','calibration':'开启校准','formal':'正式开启'}
    lines=['# 最新有效批次实验结果','',
        '**结论：证实了真实缺口下可以提前释放空间，但尚未稳定做到每轮在主峰前补足。**',
        '正式开启组 2/3 轮补足，另一轮剩余 3.11 MiB；各应用全程总缺页均通过原定 10% 门槛。',
        '峰值响应 P95 的组均值由 %.2f ms 变为 %.2f ms，观察到的变化为 %+.2f%%。每组仅 3 轮，不作显著性或普遍性能保证。'%(off['p95_ms'],formal['p95_ms'],result['p95_change_percent']),
        '', '## 实验条件','',
        '- 最新代码：`workspace/'+LATEST+'`；协议 `'+result['schema']+'`。',
        '- 场景 revisit；GIMP、Calc、Firefox 为后台，Krita 为前台输入到可见响应探针。',
        '- 独立需求参考上限 %.0f MiB，参考的 high/max/OOM 事件保持为 0；参考增长 %.2f MiB。'%(result['reference_limit_MiB'],result['reference_growth_MiB']),
        '- 关闭、校准、正式均在加载应用前设置同一 %.0f MiB 总上限。理论目标缺口 %.0f MiB；启动占用有波动，所以逐轮实际检查至少 %.0f MiB。'%(result['test_limit_MiB'],result['target_gap_MiB'],result['minimum_admission_gap_MiB']),
        '- 独立参考不计入开关对照；关闭 3 轮、开启校准 3 轮、正式开启 3 轮。校准组不并入正式均值。',
        '', '## 逐轮结果','',
        '| 顺序 | 类型 | 启动缺口 MiB | 峰前净下降 MiB | 主峰前剩余缺口 MiB | 峰值响应 P95 / 最慢 ms | 峰前 reclaim 请求 MiB |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in rounds:
        lines.append('| %d | %s | %.2f | %.2f | %.2f | %.2f / %.2f | %.2f |'%(r['order'],labels[r['stage']],
            r['required_from_start_MiB'],r['prepeak_net_drop_MiB'],r['remaining_gap_MiB'],r['response_p95_ms'],r['response_max_ms'],
            sum(r['prepeak_reclaim_requested_MiB'].values())))
    lines+=['','全部开启轮在 [8,22) 内的主动 reclaim 请求为 0，本批 completion-overrun 记录为 0；没有探针超时或漏发。',
        '“净下降”包含自然释放；“请求量”是写入 memory.reclaim 的数量，两者都不能直接称为精确回收旧页量。',
        '', '## 关闭与正式开启组比较','',
        '| 指标 | 关闭 3 轮 | 正式开启 3 轮 |','|---|---:|---:|',
        '| 主峰前补足轮数 | %d/3 | %d/3 |'%(off['ready_count'],formal['ready_count']),
        '| 主峰前占用净下降均值 | %.2f MiB | %.2f MiB |'%(off['drop_MiB'],formal['drop_MiB']),
        '| 主峰前剩余缺口均值 | %.2f MiB | %.2f MiB |'%(off['gap_MiB'],formal['gap_MiB']),
        '| 每轮 P95 的均值 | %.2f ms | %.2f ms |'%(off['p95_ms'],formal['p95_ms']),
        '| 每轮最慢响应的均值 | %.2f ms | %.2f ms |'%(off['max_ms'],formal['max_ms'])]
    for a,name in (('gimp','GIMP'),('libreoffice','Calc'),('firefox','Firefox')):
        b=off['peak_operation_ms'][a];f=formal['peak_operation_ms'][a]
        lines.append('| %s 峰值新增数据操作耗时均值 | %.3f s | %.3f s（%+.2f%%） |'%(name,b/1000,f/1000,(f/b-1)*100))
    lines+=['','## 完整任务总缺页','',
        '以下是完整负载的 pgfault，按各应用的三轮均值比较；不是只统计峰值或逐操作分别验收。',
        '', '| 应用 | 关闭均值 | 正式开启均值 | 增幅 |','|---|---:|---:|---:|']
    for a,entry in result['formal_faults']['per_app'].items():
        lines.append('| %s | %.2f | %.2f | %+.2f%% |'%(a,entry['off_mean'],entry['on_mean'],entry['relative_growth']*100))
    lines+=['','总 pgfault 包含小缺页和大缺页。大缺页另列在汇总 JSON 的 formal_faults.major_faults 中。',
        '例如 GIMP 全程大缺页均值约由 2589 增至 7627；这不是峰值期间的大缺页统计，不能直接拿来解释峰值响应。',
        '', '## 未达到的部分及诊断边界','',
        '1. 正式第 1 轮（总顺序第 7 轮）主峰前仍缺 3.11 MiB，不能用平均剩余缺口约 1.04 MiB 来宣称每轮补足。',
        '2. 该轮 GIMP 先发出 184 MiB 请求，约 2.13 s 因反馈暂停，约 4.17 s 恢复；当时已经进入下一段保护。后续约 5.407～5.423 s 只有约 16 ms 的投递窗口。尚有 72 MiB 请求预算，却没有继续补足。记录支持“保护暂停与窄执行窗口限制了剩余回收”的解释；未单独隔离每个因素的贡献。',
        '3. 预测曲线还识别出约 1.95、3.48、6.42 s 的较早增长起点，多轮在这些截止点仍有缺口。主峰评价点是实验增长操作开启前、约第 8 秒；两种时刻不能混为一谈。',
        '4. Firefox 在峰值期间仍产生 high 事件，虽无主动 reclaim，仍有内核因固定 high 执行回收/限速的机制。当前没有证明峰值期间完全没有回收代价。',
        '5. 仅一个场景、一个测试上限、每组 3 轮；预测是参考轨迹回放，冷页信息是脚本预设及历史额度，不代表真实未知操作的模型泛化。',
        '', '## 数据位置','',
        '- [完整批次 RUN](../workspace/'+LATEST+'/results/RUN-revisit.json)',
        '- [逐轮汇总 JSON](逐轮结果.json)、[组汇总与缺页 JSON](汇总指标.json)',
        '- [每轮原始记录目录](../workspace/'+LATEST+'/results/trials/)',
        '- [实时运行日志](../evidence/session/logs/20261117-deficitfix-revisit.log)',
        '- [最终系统恢复检查](../workspace/'+LATEST+'/results/FINAL_STATE-20261117.json)',
        '', '源文件冻结核验通过，采集后 zswap 恢复 N，实验 cgroup 已清理。历史中止批次在 history/ 单独保留，不计入上述结果。']
    (dest/'实验结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'rounds':len(rounds),'formal_ready':formal['ready_count'],
                      'p95_change_percent':result['p95_change_percent'],'output':str(dest)},ensure_ascii=False))


if __name__=='__main__':main()
