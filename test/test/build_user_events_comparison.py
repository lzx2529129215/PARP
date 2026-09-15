#!/usr/bin/env python3
"""Create a source-versus-replay workbook using standard-library OOXML only."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
APPS = {'FIREFOX':'Epiphany 浏览器（FIREFOX 键）', 'FALKON':'Falkon 浏览器',
        'WPS':'WPS Office', 'LIBREOFFICE':'LibreOffice', 'FILES':'Nautilus 文件管理器',
        'IMAGE_VIEWER':'EOG 图片查看器', 'GIMP':'GIMP', 'VLC':'VLC',
        'FILE_ROLLER':'File Roller', 'MOUSEPAD':'Mousepad', 'SHOTCUT':'Shotcut',
        'RHYTHMBOX':'Rhythmbox', 'KAIDAN':'Kaidan（离线）',
        'CONTROL_CENTER':'GNOME 设置', 'SOFTWARE':'GNOME Software'}
STATUS = {'VERIFIED':'后置校验通过', 'INPUT_SENT':'仅输入已送达', 'SKIPPED':'明确跳过',
          'FAILED':'执行失败', 'SKIPPED_PREREQUISITE':'前置失败后跳过'}
VERIFY = {'scroll_offset':'网页滚动位置按指定方向变化', 'page_load':'本地网页加载反馈',
          'owned_window_input_only':'仅确认输入目标为本次所属窗口', 'none':'无执行校验',
          'desktop_command':'桌面切换命令已返回', 'screenshot_file':'截图文件已生成',
          'owned_window':'本次所属应用窗口已出现', 'document_title':'目标文档标题已匹配',
          'visible_tab':'目标页面可见性反馈', 'mpris_playing':'播放器报告 Playing 状态',
          'image_header':'OCR 核对图片文件名', 'tab_input_and_known_page':'标签内容身份和可见性反馈',
          'copied_file_count':'目标目录已复制 180 个文件', 'close_input':'关闭输入已发送并记录剩余窗口',
          'native_command_artifact':'归档成员数或解压内容校验', 'saved_file':'保存文件及内容/格式校验',
          'filter_ui_input':'筛选快捷键已发送；最终文件另有 autoFilter 审计',
          'recorder_running':'独立桌面录屏进程已启动', 'recording_media':'录屏媒体文件校验',
          'saved_note_content':'笔记保存内容校验', 'four_timeline_entries':'工程时间线包含四段素材',
          'export_media':'导出帧率、时间线时长、导出进程与整段解码校验'}


def current_action(e, r):
    op, p = e['operation'], e['params']
    name = Path(r.get('document') or r.get('path') or '').name
    if op == 'skip': return '不执行，保留原始记录'
    if op == 'document_open': return '打开本地替代文档：' + name
    if op in {'scroll', 'web_scroll'}:
        return ('本地网页' if op == 'web_scroll' else '当前文档/目录') + ('向下' if p['direction'] > 0 else '向上') + '滚动'
    if op == 'web_open': return ('新建标签并打开' if p['new_tab'] else '打开/返回') + '本地替代内容页（原网址/在线内容未还原）'
    if op == 'web_tab': return f'切换到第 {p["index"]} 个来源内容标签（按页面身份定位）'
    if op == 'image_open': return f'打开本地图片 image-{(p["index"]-1)%30+1:02d}.png'
    if op == 'directory': return '进入独立素材目录：' + p['directory']
    if op == 'desktop': return '显示独立桌面' if p['show'] else '收起独立桌面'
    if op == 'resize': return '最大化窗口' if p['large'] else '缩小窗口'
    if op == 'tile': return '窗口平铺至' + ('左侧' if p['side']=='left' else '右侧')
    if op == 'key': return '发送快捷键：' + '、'.join(p['keys'])
    if op == 'zoom': return '放大视图' if p['zoom_in'] else '缩小视图'
    if op == 'bold': return '发送加粗切换快捷键（原目标：' + ('加粗' if p['bold'] else '取消加粗') + '）'
    labels = {
        'launch':'启动或激活本次专用应用窗口（不宣称原始冷启动）', 'close':'发送关闭窗口操作',
        'fullscreen':'切换全屏状态', 'filter':'发送表格自动筛选快捷键并保存',
        'save':'保存当前文件', 'save_as_dialog':'打开另存为对话框',
        'save_as_path':'将保存位置替换为独立素材目录并输入新文件名',
        'save_as_confirm':'确认另存为并校验输出：' + name,
        'search_files':'在本地文件目录搜索 document', 'sort_files':'切换列表并点击名称列排序',
        'copy_folder':'复制本地 4k 素材目录至独立 Desktop/copy 目录',
        'select_archive':'在素材目录选择归档对象', 'archive_open':'激活 File Roller 并关闭当前菜单',
        'archive_create':'通过 File Roller 创建 ZIP，校验 180 个文件成员',
        'archive_extract':'通过 File Roller 解压并核对 payload.txt 内容',
        'image_layer':'将本地 image-02.png 作为图层打开',
        'music_play':'通过 MPRIS 播放本地音频并查询播放状态',
        'video_play':'通过 MPRIS 播放本地视频并查询播放状态',
        'note_paste':'输入预设本地工作摘要，替代缺失的 AI 返回内容',
        'note_save':'保存笔记并校验预设文本内容',
        'open_dialog':'通过快捷键打开文件选择对话框',
        'screenshot':'截取本次独立桌面并保存图片',
        'record_start':'启动本次独立桌面录屏', 'record_stop':'结束录屏并校验媒体文件',
        'slideshow':'从头播放本地替代 PPT', 'shotcut_open':'在 Shotcut 打开本地 clip.mp4',
        'shotcut_play':'短时播放本地视频后暂停（压缩等待）',
        'shotcut_append':'向 Shotcut 时间线追加一段本地素材（替代素材栏操作）',
        'shotcut_export_panel':'保存工程、核对四段时间线并打开导出面板',
        'shotcut_fps':'将导出帧率设为 60 FPS',
        'shotcut_export':'导出四段本地视频并验证帧率、时长和整段解码',
    }
    return labels[op]


def method(e, r):
    if e['operation']=='skip': return '未执行'
    if r.get('lifecycle_substitution'):
        return '原生命令重启应用并加载文件'
    if r.get('gui') is False:
        return 'MPRIS 播放控制命令' if e['operation'] in {'music_play','video_play'} else '应用原生命令'
    if e['operation'] in {'desktop','resize','tile'}: return '窗口管理命令'
    if e['operation'] in {'screenshot','record_start','record_stop'}: return '独立桌面截图/录屏工具'
    if e['operation']=='launch': return '原生命令启动或激活窗口'
    return '所属窗口内按键/鼠标操作'


def difference(e, r):
    notes = [e['reason']]
    if e['operation']=='skip': return e['reason']
    if e['identity_corrected']: notes.append('根据操作文本/组内上下文调整目标应用归属')
    if r.get('lifecycle_substitution'):
        notes.append('通过重启专用实例打开文件；不保留此前窗口/文档标签状态')
    if e['operation']=='launch': notes.append('可复用已启动应用，不验证冷启动条件')
    if r.get('preparation'): notes.append('执行了额外本地素材准备：' + r['preparation']['reason'])
    notes.append('压缩原始等待；不等价于原始文件规模/内存负载')
    return '；'.join(notes)


def col(n):
    s=''
    while n:
        n, k=divmod(n-1,26);s=chr(65+k)+s
    return s


def sheet_xml(rows, widths, freeze=0, status_col=None, filtered=True):
    def el(parent, tag, attrs=None): return ET.SubElement(parent,'{'+NS+'}'+tag,attrs or {})
    root=ET.Element('{'+NS+'}worksheet')
    views=el(root,'sheetViews');view=el(views,'sheetView',{'workbookViewId':'0'})
    el(view,'pane',{'xSplit':str(freeze),'ySplit':'1','topLeftCell':col(freeze+1)+'2',
                    'activePane':'bottomRight' if freeze else 'bottomLeft','state':'frozen'})
    el(root,'sheetFormatPr',{'defaultRowHeight':'38'})
    cols=el(root,'cols')
    for i,w in enumerate(widths,1):el(cols,'col',{'min':str(i),'max':str(i),'width':str(w),'customWidth':'1'})
    data=el(root,'sheetData')
    for i,row in enumerate(rows,1):
        node=el(data,'row',{'r':str(i),'ht':'30' if i==1 else ('58' if status_col else '44'),'customHeight':'1'})
        for j,value in enumerate(row,1):
            style=1 if i==1 else (2 if i%2==0 else 3)
            if i>1 and j==status_col:style={'后置校验通过':4,'仅输入已送达':5,'明确跳过':6,'执行失败':7,'前置失败后跳过':7}.get(value,style)
            c=el(node,'c',{'r':col(j)+str(i),'s':str(style)})
            if isinstance(value,(int,float)) and not isinstance(value,bool):el(c,'v').text=str(round(value,3))
            else:
                c.set('t','inlineStr');t=el(el(c,'is'),'t');t.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
                t.text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(value if value is not None else ''))
    if filtered:el(root,'autoFilter',{'ref':f'A1:{col(len(rows[0]))}{len(rows)}'})
    el(root,'pageMargins',{'left':'.25','right':'.25','top':'.5','bottom':'.5','header':'.2','footer':'.2'})
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def styles():
    fills=['none','gray125','17365D','EDF3F8','FFFFFF','E2F0D9','FFF2CC','E7E6E6','FCE4D6']
    fill_xml=''.join('<fill><patternFill patternType="'+x+'"/></fill>' if i<2 else '<fill><patternFill patternType="solid"><fgColor rgb="FF'+x+'"/><bgColor indexed="64"/></patternFill></fill>' for i,x in enumerate(fills))
    xfs=''.join(f'<xf numFmtId="0" fontId="{1 if i==1 else 0}" fillId="{f}" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>' for i,f in enumerate([0,2,3,4,5,6,7,8]))
    return f'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="9">{fill_xml}</fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="8">{xfs}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''


def build(out, dest):
    plan=json.loads((out/'mapping/plan.json').read_text())
    audit=json.loads((out/'audit.json').read_text())
    assert audit['audit_passed'], 'Replay audit must pass before publishing a comparison'
    results=[json.loads(l) for g in sorted(out.glob('group-*')) for l in (g/'results.jsonl').read_text().splitlines()]
    assert [r['excel_row'] for r in results]==[e['excel_row'] for e in plan['events']]
    assert hashlib.sha256(Path(plan['source']).read_bytes()).hexdigest()==plan['source_sha256']
    byrow={r['excel_row']:r for r in results};counts=Counter(r['status'] for r in results)
    def target(e): return APPS[e['target_app_key']] if e['target_app_key'] else ('未执行' if e['operation']=='skip' else '独立桌面（Openbox/截图/录屏）')
    detail=[['组号','原 Excel 行号','原始应用','原始操作','当前应用/环境','当前实际操作或替代动作','当前执行方式','最终执行结果','校验依据','差异与限制/跳过原因',
             '原始操作类型','原始时间','原始间隔（秒）','本次步骤耗时（秒）','本次开始时间（北京时间）','本次结束时间（北京时间）',
             '来源数据集','来源行号','来源文件','压缩包内文件','当前操作代码','当前应用 ID','实际参数（JSON）','截图证据路径','产物/文档路径','执行日志路径']]
    for e in plan['events']:
        r=byrow[e['excel_row']];g=out/f'group-{e["group_index"]:02d}'
        local=lambda n:datetime.fromtimestamp(n,ZoneInfo('Asia/Shanghai')).isoformat(timespec='milliseconds')
        detail.append([e['group_index'],e['excel_row'],e['app_name'],e['event1'],target(e),current_action(e,r),method(e,r),STATUS[r['status']],
                       VERIFY.get(r.get('verification'),r.get('verification','')),difference(e,r),e['event2'],e['timestamp_str'],e['source_delay_s'],
                       r['completed_at']-r['started_at'],local(r['started_at']),local(r['completed_at']),e['source_dataset_id'],e['source_row_number'],
                       e['source_file'],e['source_zip_member'],e['operation'],e['runtime_app_id'],json.dumps(e['params'],ensure_ascii=False),
                       str(g/r['screenshot']) if r.get('screenshot') else '',r.get('path') or r.get('document') or '',str(g/'results.jsonl')])
    app=[['原始应用','原始记录数','当前应用/环境（按动作）','进入替代执行','后置校验通过','仅输入已送达','明确跳过','执行失败','前置失败后跳过','主要跳过原因']]
    for name,total in Counter(e['app_name'] for e in plan['events']).most_common():
        es=[e for e in plan['events'] if e['app_name']==name];c=Counter(byrow[e['excel_row']]['status'] for e in es)
        reasons=Counter(e['reason'] for e in es if e['operation']=='skip')
        app.append([name,total,'；'.join(dict.fromkeys(target(e) for e in es if e['operation']!='skip')) or '未接入',
                    sum(e['disposition']!='skip' for e in es),*[c[s] for s in STATUS], '；'.join(f'{k}（{v} 条）' for k,v in reasons.items())])
    group=[['组号','来源数据集','原始记录数','Excel 起始行','Excel 结束行','后置校验通过','仅输入已送达','明确跳过','执行失败','前置失败后跳过','进程清理']]
    for g in plan['groups']:
        es=[e for e in plan['events'] if e['group_index']==g['index']];c=Counter(byrow[e['excel_row']]['status'] for e in es)
        ga=next(x for x in audit['groups'] if x['index']==g['index'])
        group.append([g['index'],g['source_dataset_id'],len(es),es[0]['excel_row'],es[-1]['excel_row'],*[c[s] for s in STATUS],'通过' if ga['cleanup_passed'] else '未通过'])
    overview=[['项目','数量/状态','说明'],['对比范围',len(results),'逐行对比原始 Excel 与最终保留的完整回放结果；原顺序未删改。'],
              ['来源应用',len(app)-1,'同一来源应用可按操作映射到多个当前应用。'],['来源分组',len(group)-1,'各组使用独立桌面和素材副本，组内保持源顺序。']]
    overview += [[STATUS[s],counts[s],{'VERIFIED':'有后置校验；校验范围以逐行依据为准。','INPUT_SENT':'仅确认输入已送达窗口，不代表业务结果已验证。','SKIPPED':'按第一阶段范围或缺失素材/平台能力明确跳过。','FAILED':'最终结果中的执行异常。','SKIPPED_PREREQUISITE':'因前置操作失败而保留但未继续执行。'}[s]] for s in STATUS]
    overview += [['审计','通过' if audit['audit_passed'] else '未通过','核对来源哈希、行数/顺序、输出文件及进程清理；不提升 INPUT_SENT 的验证等级。'],
                 ['素材与时序','本地同类替代、压缩等待','不能据此声称原应用在线服务、原文件规模、冷启动或内存负载已完全还原。步骤耗时不等于原始间隔。'],
                 ['WPS / EOG 打开方式','部分步骤原生命令重启','以每行实际日志为准；原生命令重开会改变应用生命周期，不保留之前的窗口/文档标签。'],
                 ['第二阶段','未接入','虚拟机、会议、AI 助手等仍按行跳过。'],
                 ['统计口径','最终完整组','旧的失败尝试保存在 attempts/，此表不会将旧尝试累计为新的原始记录。'],
                 ['开始/结束时间','北京时间 UTC+08:00','源时间文本原样保留；本次执行时间由日志 Unix 时间戳转换。'],
                 ['原始文件',plan['source'],'原文件未修改。'],['源文件 SHA-256',plan['source_sha256'],'生成时再次核对。'],
                 ['当前结果目录',str(out),'逐行表的截图、产物和日志路径可用于追溯。']]
    widths=[8,12,17,53,29,64,28,21,44,83,15,28,16,18,33,33,68,12,75,75,25,12,50,90,90,90]
    sheets=[('说明与汇总',overview,[27,63,105],0,None,False),('应用对比',app,[19,15,70,17,17,17,14,14,20,110],1,None,True),
            ('分组汇总',group,[9,72,16,16,16,18,18,15,15,22,16],1,None,True),
            ('逐行对比',detail,widths,4,8,True),('跳过清单',[detail[0]]+[row for row in detail[1:] if row[7] in ['明确跳过','前置失败后跳过']],widths,4,8,True)]
    dest.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        parts=['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>','<Default Extension="xml" ContentType="application/xml"/>',
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
        parts += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1)]
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'+''.join(parts)+'</Types>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book=ET.Element('workbook',{'xmlns':NS,'xmlns:r':REL});sh=ET.SubElement(book,'sheets')
        for i,(name,rows,w,freeze,sc,filtered) in enumerate(sheets,1):
            ET.SubElement(sh,'sheet',{'name':name,'sheetId':str(i),'r:id':f'rId{i}'})
            z.writestr(f'xl/worksheets/sheet{i}.xml',sheet_xml(rows,w,freeze,sc,filtered))
        z.writestr('xl/workbook.xml',ET.tostring(book,encoding='utf-8',xml_declaration=True))
        rels=''.join(f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}<Relationship Id="rId6" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',styles())
    with zipfile.ZipFile(dest) as z:
        assert z.testzip() is None
        for name in z.namelist():ET.fromstring(z.read(name))
        root=ET.fromstring(z.read('xl/worksheets/sheet4.xml'))
        actual=root.find('{'+NS+'}sheetData')
        assert len(actual)==len(results)+1
        assert Counter(row[7].find('{'+NS+'}is/{'+NS+'}t').text for row in list(actual)[1:])==Counter(STATUS[r['status']] for r in results)
        for node,e in zip(list(actual)[1:],plan['events']):
            assert node[2].find('{'+NS+'}is/{'+NS+'}t').text==e['app_name']
            assert node[3].find('{'+NS+'}is/{'+NS+'}t').text==e['event1']
    return {'path':str(dest),'sheets':{s[0]:len(s[1])-1 for s in sheets},'counts':dict(counts),'bytes':dest.stat().st_size}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--results',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(build(a.results.resolve(),a.output.resolve()),ensure_ascii=False,indent=2))
