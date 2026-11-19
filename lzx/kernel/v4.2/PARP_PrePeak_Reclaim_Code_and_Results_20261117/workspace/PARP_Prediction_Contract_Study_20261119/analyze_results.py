"""Audit preserved native outputs and sources; build a compact reproducible report."""
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
R = ROOT/'results'
MIB = 1024**2


def load(p): return json.loads(p.read_text())


def main():
    rows, sources, batches = [], [], []
    for folder in sorted(R.glob('native-*')):
        if not (folder/'PLAN.json').exists(): continue
        plan, summary = load(folder/'PLAN.json'), load(folder/'SUMMARY.json')
        assert summary['status'] == 'COMPLETE', (folder,summary['status'])
        matches = []
        for archive in (R/'source_archives').iterdir():
            if all((archive/name).exists() and hashlib.sha256((archive/name).read_bytes()).hexdigest() == value
                   for name,value in plan['source_sha256'].items()): matches.append(archive.name)
        assert matches, 'executed source snapshot missing: '+folder.name
        assert plan['controller_sha256'] == load(ROOT/'source_baseline.json')['controller_sha256']
        sources.append({'batch':folder.name,'matching_executed_source':matches})
        assert len(summary['trials']) == len(plan['specs'])
        for item in summary['trials']:
            spec = item['spec']; data = load(folder/spec['tag']/'result.json')
            assert data['passed']
            frames = data['pipeline']['frames']
            assert not data['pipeline']['errors']
            for frame in frames:
                for app,reply in frame['intra_app'].items():
                    assert abs(reply['issued_s']-frame['time_s']) < 1e-7
                    assert len(reply['windows']) == round(spec['horizon_s']/.5)
                    assert abs(reply['windows'][0]['start_s']-frame['time_s']) < 1e-7
                    assert abs(reply['windows'][-1]['end_s']-frame['time_s']-spec['horizon_s']) < 1e-7
                    assert all(o['time_s'] <= frame['time_s'] for o in frame['requests'][app]['observations'])
                    if spec['variant'].endswith('faults'):
                        assert all(type(w['pgfault']) is int and w['pgfault'] >= 0 for w in reply['windows'])
            for events in data['pipeline']['executor_events'].values():
                used = [e['used_bytes'] for e in events if 'used_bytes' in e]
                assert used == sorted(used), 'attempt budget was reset'
            if data['status'] == 'CORRECTLY_INFEASIBLE':
                rows.append({'batch':folder.name,'tag':spec['tag'],'scenario':spec['scenario'],
                             'status':data['status'],'advance':True,'passed':True})
                continue
            assert not data['bad_reclaim_calls'] and not data['during_peak_reclaim_calls']
            assert not data['peak_events']['oom_kill']
            if spec['advance']: assert data['max_preallocation_gap_bytes'] == 0
            else: assert not data['reclaim_calls']
            major = sum(e['response']['major_delta'] for e in data['workload_events'])
            rows.append({'batch':folder.name,'tag':spec['tag'],'scenario':spec['scenario'],
                         'status':'COMPLETE','advance':spec['advance'],'variant':spec['variant'],
                         'H':spec['horizon_s'],'gap_MiB':data['max_preallocation_gap_bytes']/MIB,
                         'peak_ms':data['peak_allocation_ms'],'peak_cpu_ms':data['peak_allocation_cpu_ms'],
                         'major_faults':major,'max_events':data['peak_events']['max'],
                         'reclaim_MiB':sum(w['requested_bytes'] for w in data['reclaim_calls'])/MIB,
                         'last_reclaim_s':max((w['end_s'] for w in data['reclaim_calls']),default=None),
                         'lead_qualified':data['conservative_lead_qualified'],'passed':True})
        batches.append({'batch':folder.name,'trials':len(summary['trials']),
                        'feedback_fix':plan.get('feedback_fix'),'warmup':plan.get('warmup_before_formal',False)})
    full = [r for r in rows if r['status']=='COMPLETE']
    ideals = [r for r in full if r['advance']]
    result = {'status':'VERIFIED','native_checks':len(rows),'complete_trials':len(full),
              'ideal_complete_trials':len(ideals),'correctly_infeasible':len(rows)-len(full),
              'all_ideal_preallocation_gaps_zero':all(r['gap_MiB']==0 for r in ideals),
              'batches':batches,'source_matches':sources,'rows':rows,
              'limitations':['synthetic whole-app clean-file donation; not anonymous compression',
                             'no real predictor training or history-length optimality established',
                             'fixed high supplied by independent full-session reference envelope',
                             'latency varies, including no-reclaim controls; do not attribute all paired differences to reclamation',
                             'per-window forecasts have 0.5-second resolution; subwindow unobserved peaks are outside the verified scope']}
    (R/'VERIFIED.json').write_text(json.dumps(result,indent=2)+'\n')
    lines = ['# 真实虚拟机结果与边界','',f"共 {len(rows)} 项真实检查：{len(full)} 轮完整运行、{len(rows)-len(full)} 次正确报告安全冷量不足。",
             f"其中 {len(ideals)} 轮理想预测完整运行的每次分配前缺口均为 0；无热应用主动回收、无增长保护期主动回收、无 OOM。",'',
             '|批次/场景|模式|范围 s|分配前最大缺口 MiB|各次峰值申请 ms|保守提前量合格|',
             '|---|---|---:|---:|---|---|']
    for r in rows:
        if r['status']!='COMPLETE':continue
        mode='理想' if r['advance'] else '当前状态对照'
        lines.append(f"|{r['batch']}/{r['tag']}|{mode}|{r['H']:g}|{r['gap_MiB']:.2f}|{', '.join(f'{x:.2f}' for x in r['peak_ms'])}|{r['lead_qualified']}|")
    lines += ['', '12 秒范围在本次硬件上实际补齐，但保守校准的整段时间预算不合格；20、30 秒合格，因此当前默认选 20 秒，不称为普适最优。',
              '首批第一对照出现 1156 ms、CPU 约 1147 ms、内存 PSI 约 3.5 ms；不能全部归因于回收。后两批增加不计分的完整申请预热，并保留 CPU/墙钟。',
              '无主动回收的对照中也存在明显时间波动。空间补足结论稳定，峰值申请有改善的案例，但当前不宣称稳定的统一加速百分比。',
              '首批为峰值编号修正；第二批复验实际反馈扣除修正；第三批确认按用户要求保留的逐窗口缺页输出。三批代码快照均已按 PLAN 哈希匹配保存。',
              '当前输出保留 WSS、占用增量、缺页次数，以及安全冷量/有效期。缺页字段是单个未来窗口的计数，不重复累计。',
              '固定 high 仍由实验全序列参考配置给出；本批未证明如何仅凭滚动 20 秒输出自动得到全序列最大 high。',
              '数据为整应用冷热的合成干净文件页，不代表页级冷热模型、匿名压缩、真实应用 GUI 卡顿或真实预测准确率已经验证。',
              '详细机器可读证据：results/VERIFIED.json、各 native-*/PLAN.json/SUMMARY.json/result.json、source_archives/。','']
    (ROOT/'实验结果.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('status','native_checks','complete_trials','ideal_complete_trials','correctly_infeasible')},ensure_ascii=False))


if __name__=='__main__': main()
