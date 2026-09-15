#!/usr/bin/env python3
"""Project application sequences directly from the comparison workbook."""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import xml.etree.ElementTree as E
import zipfile

from build_user_events_comparison import NS, REL, sheet_xml, styles


def read_rows(path):
    ns={'s':NS}
    with zipfile.ZipFile(path) as z:
        book=E.fromstring(z.read('xl/workbook.xml'))
        sheet=next(s for s in book.find('s:sheets',ns) if s.attrib['name']=='逐行对比')
        links=E.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target=next(r.attrib['Target'] for r in links if r.attrib['Id']==sheet.attrib['{'+REL+'}id'])
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=[''.join(s.itertext()) for s in E.fromstring(z.read('xl/sharedStrings.xml'))]
        rows=[]
        for row in E.fromstring(z.read(target.lstrip('/') if target.startswith('/') else 'xl/'+target)).find('s:sheetData',ns):
            cells={}
            for c in row:
                value=c.findtext('s:v','',ns)
                if c.attrib.get('t')=='inlineStr':value=''.join(c.find('s:is',ns).itertext())
                elif c.attrib.get('t')=='s':value=shared[int(value)]
                key=''.join(x for x in c.attrib['r'] if x.isalpha());cells[key]=value
            rows.append(cells)
    header=rows[0]
    return [{name:r.get(key,'') for key,name in header.items()} for r in rows[1:]]


def collapse(rows, current=True):
    runs=[];excluded=Counter()
    for r in rows:
        if current:
            if r['最终执行结果'] not in {'后置校验通过','仅输入已送达'}:
                excluded['未执行/失败记录']+=1;continue
            if not r['当前应用 ID']:
                excluded['桌面/截图/录屏记录']+=1;continue
            app=r['当前应用/环境'];key=r['当前应用 ID']
        else:
            if r['原始应用'] in {'桌面','应用中心'}:
                excluded['原始桌面/应用中心标签']+=1;continue
            app=r['原始应用'];key=app
        group=int(r['组号']);source_row=int(r['原 Excel 行号'])
        if runs and (runs[-1]['group'],runs[-1]['key'])==(group,key):
            runs[-1]['rows'].append(source_row)
        else:runs.append(dict(group=group,key=key,app=app,rows=[source_row],dataset=r['来源数据集']))
    assert all((a['group'],a['key'])!=(b['group'],b['key']) for a,b in zip(runs,runs[1:]))
    assert sum(len(r['rows']) for r in runs)+sum(excluded.values())==len(rows)
    return runs,excluded


def tables(runs, current):
    nodes=[['组号','组内序号','应用','应用 ID' if current else '原始标签','合并记录数','原 Excel 首行','原 Excel 末行','包含的原 Excel 行号','来源数据集']]
    edges=[['组号','组内切换序号','从应用','到应用','前一应用最后记录行','后一应用首次记录行','来源数据集']]
    group_counts=Counter();switch_counts=Counter();previous=None
    for r in runs:
        g=r['group'];group_counts[g]+=1
        nodes.append([g,group_counts[g],r['app'],int(r['key']) if current else r['key'],len(r['rows']),r['rows'][0],r['rows'][-1],','.join(map(str,r['rows'])),r['dataset']])
        if previous and previous['group']==g:
            switch_counts[g]+=1
            edges.append([g,switch_counts[g],previous['app'],r['app'],previous['rows'][-1],r['rows'][0],r['dataset']])
        previous=r
    return nodes,edges


def write_book(path,sheets):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        parts=['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>','<Default Extension="xml" ContentType="application/xml"/>',
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        parts += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1)]
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'+''.join(parts)+'</Types>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book=E.Element('workbook',{'xmlns':NS,'xmlns:r':REL});sh=E.SubElement(book,'sheets')
        for i,(name,rows,widths) in enumerate(sheets,1):
            E.SubElement(sh,'sheet',{'name':name,'sheetId':str(i),'r:id':f'rId{i}'})
            z.writestr(f'xl/worksheets/sheet{i}.xml',sheet_xml(rows,widths,freeze=1))
        z.writestr('xl/workbook.xml',E.tostring(book,encoding='utf-8',xml_declaration=True))
        rels=''.join(f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}<Relationship Id="rId{len(sheets)+1}" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',styles())
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        for p in z.namelist():E.fromstring(z.read(p))


def build(source,output):
    rows=read_rows(source);current,excluded=collapse(rows);original,original_excluded=collapse(rows,False)
    cn,ce=tables(current,True);on,oe=tables(original,False)
    groups=list(dict.fromkeys(int(r['组号']) for r in rows))
    summary=[['组号','当前应用序列（→ 表示切换）','当前应用段数','当前切换次数','原始标注序列（仅作对照）','原始应用段数','原始切换次数']]
    for g in groups:
        c=[r['app'] for r in current if r['group']==g];o=[r['app'] for r in original if r['group']==g]
        summary.append([g,' → '.join(c),len(c),max(0,len(c)-1),' → '.join(o),len(o),max(0,len(o)-1)])
    notes=[['项目','值/口径'],['输入文件',str(source)],['输入 SHA-256',hashlib.sha256(source.read_bytes()).hexdigest()],
           ['源记录数',len(rows)],['分组数',len(groups)],['当前应用段数',len(current)],['当前应用间切换次数',len(ce)-1],
           ['合并规则','按输入表顺序，仅合并连续相同应用；A→B→A 保留。先排除非应用记录，再合并；不跨来源组连接。'],
           ['当前序列','使用“当前应用/环境”和“当前应用 ID”；仅保留后置校验通过/仅输入已送达的应用记录。'],
           ['排除的当前记录','；'.join(f'{k}：{v} 条' for k,v in excluded.items())],
           ['原始标注对照','按“原始应用”列合并；排除“桌面”“应用中心”，保留原始未执行应用。此列没有纠正原数据的应用误标，不能当成实际窗口切换证据。'],
           ['排除的原始标签记录',sum(original_excluded.values())],
           ['首个应用','表示每组起点，不计作一次切换。切换明细每行只记录一个“从应用→到应用”。'],
           ['行号口径','段内首末行是覆盖边界，中间可能有被排除行；“包含的原 Excel 行号”给出精确参与合并的记录。'],
           ['结果性质','这是对比表中应用标签的投影和连续合并，不是新执行的回放，也不是从窗口焦点日志重建的真实切换。']]
    widths=[9,13,40,20,16,17,17,100,75]
    ew=[9,18,40,40,23,23,75]
    sheets=[('每组切换序列',summary,[9,120,18,18,120,18,18]),('当前应用序列',cn,widths),('当前切换明细',ce,ew),
            ('原始标注序列',on,widths),('原始标注切换',oe,ew),('处理说明',notes,[30,130])]
    output.parent.mkdir(parents=True,exist_ok=True);write_book(output,sheets)
    txt=output.with_suffix('.txt')
    txt.write_text('\n\n'.join(f'第 {r[0]} 组（{r[2]} 个应用段，{r[3]} 次切换）\n{r[1]}' for r in summary[1:])+'\n',encoding='utf-8')
    print('输出：',output,'\n纯序列：',txt)
    print('当前应用段/切换：',len(current),len(ce)-1,'；原始标注段/切换：',len(original),len(oe)-1)
    print('逐组当前段数：',[(r[0],r[2]) for r in summary[1:]])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    build(a.source.resolve(),a.output.resolve())
