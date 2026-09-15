"""Summarize actual fixed-window runs without treating short windows as comparable."""
import argparse
import html
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from automation.wps_replay import guard


def summarize(folder):
    result=json.loads((folder/'result.json').read_text())
    if result['status']!='GUI_AUDIT_PASS':raise ValueError('Cannot report failed task as success')
    windows=[json.loads(s) for s in (folder/'windows.jsonl').read_text().splitlines()]
    events=json.loads((folder/'events.json').read_text());zero=windows[0]['begin_ns']
    times={e['event']:(e['monotonic_ns']-zero)/1e9 for e in events}
    rows=[dict(start=(r['begin_ns']-zero)/1e9,end=(r['end_ns']-zero)/1e9,
        wss=r['referenced_bytes']/2**20,rss=r['rss_bytes']/2**20,
        full=not r['final_short_window'],complete=r['coverage_complete']) for r in windows if r['referenced_bytes'] is not None]
    valid=[r for r in rows if r['full'] and r['complete']]
    # Initial begin acknowledgement follows begin_ns by a few microseconds.
    before=[r['wss'] for r in valid if r['end']<=times['TASK_PREPARATION_START']]
    after=[r['wss'] for r in valid if r['start']>=times['POST_IDLE_START'] and r['end']<=times['POST_IDLE_END']]
    during=[r['wss'] for r in valid if r['end']>times['OP_START'] and r['start']<times['OP_ACTIONS_RETURNED']]
    summary=dict(task_id=result['task_id'],source=str(folder),status=result['status'],
        window_s=windows[0]['target_window_s'],
        pre_idle_s=times['TASK_PREPARATION_START']-times.get('PRE_IDLE_START',0),
        post_idle_s=times['POST_IDLE_END']-times['POST_IDLE_START'],
        baseline_median_mib=statistics.median(before) if before else None,operation_overlap_peak_mib=max(during,default=None),
        whole_trace_peak_mib=max(r['wss'] for r in valid),post_idle_median_mib=statistics.median(after) if after else None,
        last_full_rss_mib=valid[-1]['rss'],operation_s=times['OP_ACTIONS_RETURNED']-times['OP_START'],
        complete_windows=sum(r['coverage_complete'] for r in windows),total_windows=len(windows),
        baseline_windows=len(before),post_idle_windows=len(after),
        max_collection_gap_s=max(r['collection_gap_s'] or 0 for r in windows),
        quality_flags=sorted({f for r in windows for f in r['quality_flags']}),
        new_process_observations=sum(not p['precleared'] for r in windows for p in r['processes']))
    return summary,rows,times,events,zero


def run(output,sources):
    g=guard();g.check();collected=[summarize(g.path(s)) for s in sources]
    window=collected[0][0]['window_s']
    if any(c[0]['window_s']!=window for c in collected):raise ValueError('Mixed window sizes')
    names=['NOW 重新计算','按 C 列升序排列整行','按 D 列升序排列整行']
    svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="1030" viewBox="0 0 1100 1030">',
         '<rect width="1100" height="1030" fill="white"/>',
         '<style>text{font-family:DejaVu Sans,Noto Sans CJK SC,sans-serif;fill:#243247;font-size:14px}</style>',
         f'<text x="70" y="30" font-size="22">WPS 实测：{window:g} 秒窗口 WSS 与 RSS（每个场景独立实例）</text>',
         '<text x="70" y="57">蓝线 WSS · 灰线 RSS · 橙色 操作阶段 · 紫线 审计保存 · 空心点 短窗口 · 红圈 覆盖缺失</text>']
    report=['# 三条 AgentNet 衍生场景的 WPS WSS 实测','',
        '实际使用 WPS GUI，各场景独立 Xvfb/cgroup、合成小型工作簿，均通过保存文件内容校验。每场景一次成功试跑，不能据此代表大型文档或用户总体。',
        f'采样从文档加载完成后开始：前置空闲、准备及操作、校验、后置空闲，实际阶段时间见 events.json。每窗口对已有 WPS 进程 clear_refs=1，再读取 smaps；窗口约 {window:g} 秒，扫描间隔存在未覆盖时间。',
        'WSS 是窗口结束时可见 Referenced 的进程求和近似；RSS 也按进程求和，未去重共享页。退出进程与已释放映射无法完整恢复。自动化工具不计入 WPS cgroup，但操作、剪贴板检查、审计保存引发的 WPS 活动会计入。',
        '操作峰值来自与操作阶段重叠的完整窗口，可能同时覆盖准备或审计，不能解释为单独操作的精确 WSS。末尾不足目标长度的窗口仅展示，不参与统计。', '',
        '|场景|前空闲中位数|操作重叠窗口峰值|全程峰值|后空闲中位数|末次 RSS|完整覆盖窗口|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for i,(summary,rows,times,events,zero) in enumerate(collected):
        title=names[i] if len(collected)==3 else summary['task_id']
        top=100+i*300;x0=80;width=950;height=210;ymax=650;tmax=rows[-1]['end']
        x=lambda t:x0+width*t/tmax
        y=lambda value:top+height-height*value/ymax
        svg.append(f'<text x="80" y="{top-12}">{i+1}. {html.escape(title)} — MiB</text>')
        for value in range(0,651,100):
            svg.append(f'<path d="M80 {y(value)}H1030" stroke="#e2e8f0"/><text x="30" y="{y(value)+5}">{value}</text>')
        svg.append(f'<rect x="{x(times["OP_START"])}" y="{top}" width="{x(times["OP_ACTIONS_RETURNED"])-x(times["OP_START"])}" height="210" fill="#ffb347" opacity=".23"/>')
        for e in events:
            if e['event']=='AUDIT_SAVE_START':
                xx=x((e['monotonic_ns']-zero)/1e9)
                svg.append(f'<path d="M{xx} {top}v210" stroke="#a855f7" stroke-dasharray="4 4"/>')
        for field,color in [('rss','#94a3b8'),('wss','#147bd1')]:
            points=' '.join(f'{x(r["end"])},{y(r[field])}' for r in rows)
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
            for r in rows:
                svg.append(f'<circle cx="{x(r["end"])}" cy="{y(r[field])}" r="{5 if not r["complete"] else 3.5}" fill="{color if r["full"] and r["complete"] else "white"}" stroke="{color if r["complete"] else "#dc2626"}"/>')
        for tick in range(0,int(tmax)+1,max(5,int(tmax/12/5)*5)):
            svg.append(f'<text x="{x(tick)-5}" y="{top+235}">{tick}</text>')
        svg.append(f'<text x="420" y="{top+258}">文档就绪后的时间（秒）；点位于窗口结束时刻</text>')
        report.append('|'+title+'|'+ '|'.join((f'{summary[k]:.2f}' if summary[k] is not None else 'NA') for k in ['baseline_median_mib','operation_overlap_peak_mib','whole_trace_peak_mib','post_idle_median_mib','last_full_rss_mib'])+f'|{summary["complete_windows"]}/{summary["total_windows"]}|')
    svg.append('</svg>')
    report+=['','所有内存数值为 MiB。NOW 检查 F9 后保存前的界面时间推进；排序检查整行排序、表头与其他单元格完整性。',
        '本报告只汇总所列成功运行；早期失败或中断试跑保留在各自输出目录，不混入曲线。',
        '红圈为覆盖不完整窗口，其值仅为仍可观测贡献，不参与峰值及中位数统计；全程峰值也仅指有效完整窗口中的峰值。后空闲 Referenced 下降表示该时间窗内访问减少，并不证明剩余 RSS 都能立即回收。', '', '## 原始记录']
    for summary,*_ in collected:report.append(f'- [{summary["task_id"]}]({summary["source"]}/timeline.csv)：events.json、windows.jsonl、result.json 与 runtime 下逐进程 smaps。最大采集间隔 {summary["max_collection_gap_s"]:.3f} 秒；质量标记 {summary["quality_flags"]}；未预清零新进程观测 {summary["new_process_observations"]}。')
    g.write_json(output+'/summary.json',[c[0] for c in collected])
    for name,content in [('wss_timeline.svg',''.join(svg)),('report.md','\n'.join(report)+'\n')]:
        with g.open(output+'/'+name) as f:f.write(content.encode())
    print(json.dumps([c[0] for c in collected],ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('sources',nargs='+')
    a=p.parse_args();run(a.output,a.sources)
