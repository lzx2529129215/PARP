#!/usr/bin/env python3
"""Flatten the comparison into one chronological application sequence."""
import argparse
from datetime import datetime
from pathlib import Path

from build_user_events_switch_sequence import read_rows, write_book


def build(source, output):
    rows=read_rows(source)
    # Original timestamps define chronology; retries' execution times do not.
    rows.sort(key=lambda r:(datetime.strptime(r['原始时间'],'%Y-%m-%d %H:%M:%S,%f'),int(r['原 Excel 行号'])))
    kept=[r for r in rows if r['当前应用 ID'] and r['最终执行结果'] in {'后置校验通过','仅输入已送达'}]
    runs=[]
    for r in kept:
        if runs and runs[-1]['id']==r['当前应用 ID']:
            runs[-1]['rows'].append(r)
        else:runs.append({'id':r['当前应用 ID'],'app':r['当前应用/环境'],'rows':[r]})
    assert sum(len(r['rows']) for r in runs)==len(kept)
    assert all(a['id']!=b['id'] for a,b in zip(runs,runs[1:]))
    assert [r for run in runs for r in run['rows']]==kept
    text=' → '.join(r['app'] for r in runs)
    nodes=[['序号','应用','应用 ID','原始开始时间','原始结束时间','合并操作数','原 Excel 行号（按时间顺序）']]
    edges=[['切换序号','从应用','到应用','后一应用原始开始时间','前一应用最后原 Excel 行号','后一应用首次原 Excel 行号']]
    for i,r in enumerate(runs,1):
        rs=r['rows']
        nodes.append([i,r['app'],int(r['id']),rs[0]['原始时间'],rs[-1]['原始时间'],len(rs),','.join(x['原 Excel 行号'] for x in rs)])
        if i>1:
            prev=runs[i-2]
            edges.append([i-1,prev['app'],r['app'],rs[0]['原始时间'],int(prev['rows'][-1]['原 Excel 行号']),int(rs[0]['原 Excel 行号'])])
    notes=[['项目','值/规则'],['源文件',str(source)],['原始记录数',len(rows)],['进入应用序列的记录数',len(kept)],
           ['应用段数',len(runs)],['切换次数',max(0,len(runs)-1)],
           ['排序','全部记录按原始时间升序；相同时间按原 Excel 行号排序，不按重试后的实际执行时间排序。'],
           ['合并','不分组；过滤未执行和非应用记录后，连续相同应用合并，包括原分组之间的相同应用。A→B→A 保留。'],
           ['含义','按当前应用标签形成的长序列。跨来源记录的相邻关系按要求拼接，不代表原始同一次会话内的真实焦点切换。']]
    output.parent.mkdir(parents=True,exist_ok=True)
    write_book(output,[('完整长序列',[['应用间切换长序列'],[text]],[180]),
                       ('应用序列',nodes,[10,42,14,29,29,16,120]),
                       ('切换明细',edges,[14,42,42,29,29,29]),('处理说明',notes,[32,140])])
    output.with_suffix('.txt').write_text(text+'\n',encoding='utf-8')
    print(f'{len(rows)} 条原记录 → {len(kept)} 条应用记录 → {len(runs)} 个应用段 / {len(runs)-1} 次切换')
    print(output)
    print(output.with_suffix('.txt'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();build(a.source.resolve(),a.output.resolve())
