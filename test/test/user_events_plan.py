#!/usr/bin/env python3
"""Lossless XLSX import and explicit operation-level first-stage mapping."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[2]
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
HEADERS = ['timestamp', 'timestamp_str', 'event1', 'event2', 'app_name',
           'source_dataset_id', 'source_kind', 'source_file', 'source_zip_member', 'source_row_number']
BASE = {'浏览器':'FIREFOX', '海泰浏览器':'FALKON', 'WPS':'WPS', '文件管理器':'FILES',
        '图库':'SHOTWELL', '腾讯文档':'LIBREOFFICE', '应用中心':None, '桌面':None,
        '应用市场':'SOFTWARE', '悟空图像':'GIMP', '腾讯会议':None, 'QQ音乐':'RHYTHMBOX',
        '抖音':'VLC', '小艺':None, '飞书':'GAJIM', '好压':'FILE_ROLLER', '备忘录':'MOUSEPAD',
        '剪映':'SHOTCUT', '虚拟机':None, '企业微信':'KAIDAN'}

def read_workbook(path):
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(n.itertext()) for n in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        book = ET.fromstring(z.read('xl/workbook.xml'))
        sheet = next(s for s in book.find('s:sheets', NS) if s.attrib['name'] == 'user_events合并')
        rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == rid)
        member = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        rows = []
        for row in ET.fromstring(z.read(member)).findall('s:sheetData/s:row', NS):
            values = {}
            for c in row:
                col = re.match('[A-Z]+', c.attrib['r'])[0]
                value = c.find('s:v', NS)
                value = value.text if value is not None else ''
                if c.attrib.get('t') == 's': value = strings[int(value)]
                if c.attrib.get('t') == 'inlineStr': value = ''.join(c.find('s:is', NS).itertext())
                values[col] = value or ''
            if not rows:
                if [values.get(chr(65+i), '') for i in range(10)] != HEADERS:
                    raise ValueError('Unexpected spreadsheet headers')
                rows.append(None)
            elif any(values.values()):
                rows.append(dict(excel_row=int(row.attrib['r']), **{
                    h: values.get(chr(65+i), '') for i, h in enumerate(HEADERS)}))
        return rows[1:]

def classify(row, state):
    text, source = row['event1'], row['app_name']
    app = BASE.get(source)
    def mapped(op, target=app, reason='本地有限素材；保留操作类别，非原始内容/负载', **params):
        return dict(operation=op, target_app_key=target, disposition='substitute', reason=reason, params=params)
    def skip(reason): return dict(operation='skip', target_app_key=None, disposition='skip', reason=reason, params={})
    if source in {'腾讯会议', '飞书', '小艺', '虚拟机'}:
        return skip('第二阶段范围：会议/助手/虚拟机；此次不接入')
    # Desktop transitions must remain in the sequence, including those labelled WPS/Files.
    if any(s in text for s in ['进入任务中心','退出任务中心','进入应用中心','退出应用中心']):
        return mapped('desktop', None, 'Openbox 显示/收起桌面替代原任务/应用中心；保留边界', show='进入' in text)
    if source == '桌面':
        if '截图' in text: return mapped('screenshot', None, '仅截取独立回放桌面')
        if '开始录屏' in text: return mapped('record_start', None, '录制独立回放桌面；压缩原始等待')
        if '结束录屏' in text: return mapped('record_stop', None, '结束并校验独立桌面录屏')
        return skip('不模拟宿主电源、锁屏和唤醒')
    if '卸载' in text or (source == '应用市场' and '安装' in text):
        return skip('原鸿蒙应用不可用；不把目录浏览计为安装或卸载成功')
    if '中望' in text or '.dwg' in text or 'CAD' in text:
        state['cad'] = '关闭' not in text
        return skip('当前 30 应用空间没有 CAD 执行器和 DWG 原素材')
    if state.get('cad') and source in {'文件管理器','应用中心'}:
        return skip('CAD 流程的视图/平移操作不可用')
    if '迅雷' in text:
        state['download'] = True
        return skip('未提供下载链接；当前应用空间无迅雷执行器')
    if source == '应用中心' and state.get('download') and '启动' not in text:
        return skip('下载流程缺少原始链接/下载器')
    if '启动' in text:
        state['download'] = False
        launch = [('WPS','WPS'),('企业微信','KAIDAN'),('设置','CONTROL_CENTER'),('备忘录','MOUSEPAD'),
                  ('腾讯文档','LIBREOFFICE'),('图库','FILES'),('剪映','SHOTCUT'),('抖音','FIREFOX'),
                  ('悟空图像','GIMP'),('文件管理','FILES'),('文管','FILES')]
        for token, key in launch:
            if token in text:
                if key == 'FILES' and token == '图库': state['gallery'] = 'images'
                return mapped('launch', key, cold='冷启动' in text)
    if source == '企业微信':
        return mapped('close' if '关闭' in text else 'launch', 'KAIDAN', '仅打开/关闭离线聊天客户端，不登录或发消息')
    if source == '应用中心':
        if 'WPS窗口' in text: return mapped('tile', 'WPS', side='right')
        if '“图片”' in text or '“视频”' in text:
            state['gallery'] = 'videos' if '视频' in text else 'images'
            return mapped('directory', 'FILES', directory=state['gallery'])
        if '“直播”' in text: return mapped('web_open', 'FIREFOX', page='live', new_tab=False)
        if '打开文件' in text: return mapped('open_dialog', 'GIMP')
        # Mislabelled file operations are classified by their actual semantics below.
        if any(k in text for k in ['文件夹','点击更多']): source, app = '文件管理器', 'FILES'
    if source in {'浏览器','海泰浏览器','抖音'}:
        app = 'FALKON' if source == '海泰浏览器' else 'FIREFOX'
        if any(k in text for k in ['腾讯文档','上传','立即上传','演示','播放PPT','退出放映']):
            # Cloud upload cannot be claimed from opening an offline page.
            return skip('此浏览器流程依赖腾讯文档账号和上传后的云端会话；本地文档另有回放')
        if any(k in text for k in ['向下','下滑']): return mapped('web_scroll', app, direction=1)
        if any(k in text for k in ['向上','上滑']): return mapped('web_scroll', app, direction=-1)
        if '窗口化' in text or '窗口缩小' in text: return mapped('resize', app, large=False)
        if '最大化' in text or '窗口放大' in text: return mapped('resize', app, large=True)
        if '全屏' in text: return mapped('fullscreen', app, enabled='退出' not in text)
        if '切换' in text and 'tab' in text:
            n = 5 if '第五' in text else 1 if '第一' in text else int(re.search(r'\d+', text)[0])
            return mapped('web_tab', app, index=n)
        if '返回' in text or '退出直播' in text or '关闭评论' in text:
            return mapped('web_open', app, page='home', new_tab=False)
        return mapped('web_open', app, page=text, new_tab=bool(re.search('打开第', text)),
                      reason='原网页/直播/评论替换为本地长页、图片或视频；无真实服务端/网络负载')
    if source == 'WPS' or (source == '文件管理器' and 'WPS文件' in text):
        app = 'WPS'
        if '输入wps' in text: return mapped('open_dialog', app)
        if '打开文件' in text:
            component = 'ppt' if '.pptx' in text else 'sheet' if '.xlsx' in text else 'word'
            state['wps_component'] = component
            return mapped('document_open', app, document=component)
        if any(k in text for k in ['下滚','下滑','向下']): return mapped('scroll', app, direction=1, pane=state.get('wps_component','word'))
        if any(k in text for k in ['上滚','上滑','向上']): return mapped('scroll', app, direction=-1, pane=state.get('wps_component','word'))
        if '退出放映' in text: return mapped('key', app, keys=['Escape'])
        if '放映PPT' in text: return mapped('slideshow', app)
        if '点击屏幕' in text: return mapped('key', app, keys=['Right'])
        if '加粗' in text: return mapped('bold', app, bold='取消' not in text)
        if '筛选' in text: return mapped('filter', app)
        if any(k in text for k in ['插入','文本框']): return skip('源记录未提供文本内容；此次跳过空文本框及其菜单步骤')
        if '另存为' in text: return mapped('save_as_dialog', app)
        if '“文件”' in text: return mapped('key', app, keys=['alt+f'])
        if '“下载”' in text: return mapped('save_as_path', app, reason='下载目录替换为独立素材目录；输入新文件名，后续在 WPS 中另存为')
        if '保存' in text: return mapped('save_as_confirm' if '点击保存' in text else 'save', app)
    if source == '腾讯文档':
        app = 'LIBREOFFICE'
        if '打开第' in text or '切换到' in text:
            doc = 'sheet' if 'Excel' in text else 'ppt' if 'PPT' in text else 'word'
            state['lo_component'] = doc
            return mapped('document_open', app, document=doc)
        if any(k in text for k in ['向下','向上']): return mapped('scroll', app, direction=1 if '向下' in text else -1)
        if '退出放映' in text: return mapped('key', app, keys=['Escape'])
        if '点击屏幕' in text: return mapped('key', app, keys=['Right'])
        if '放映按钮' in text: return mapped('key', app, keys=['alt+s'])
        if '从头演示' in text: return mapped('slideshow', app)
        if '全选' in text: return mapped('key', app, keys=['ctrl+a'])
        if '加粗' in text: return mapped('bold', app, bold='取消' not in text)
        return skip('在线字体菜单/宋体替换依赖未提供的原字体与文档样式')
    if source in {'文件管理器', '好压'}:
        app = 'FILES'
        if '压缩' in text or '解压' in text:
            if '选中' in text: return mapped('select_archive', app)
            if '立即压缩' in text or '压缩为zip' in text: return mapped('archive_create', 'FILE_ROLLER')
            if '立即解压' in text or '解压到当前' in text: return mapped('archive_extract', 'FILE_ROLLER')
            return mapped('archive_open', 'FILE_ROLLER')
        if '双击打开' in text and '图片' in text: return mapped('image_open', 'IMAGE_VIEWER', index=int(re.search(r'\d+',text)[0]))
        if 'ESC关闭' in text: return mapped('key', 'IMAGE_VIEWER', keys=['Escape'])
        if '路径' in text or '文件夹' in text:
            if '粘贴复制' in text: return mapped('copy_folder', app)
            folder = 'Desktop' if '桌面' in text else 'testingFile' if 'testingFile' in text else '4k' if re.search(r'\dk',text) else 'testingFile'
            return mapped('directory', app, directory=folder)
        if any(k in text for k in ['向下','下滑','向上','上滑']):
            return mapped('scroll', app, direction=1 if '下' in text else -1)
        if '关闭文管' in text: return mapped('close', app)
        if '新的tab' in text: return mapped('key', app, keys=['ctrl+t'])
        if '屏幕左侧' in text: return mapped('tile', app, side='left')
        if '搜索' in text: return mapped('search_files', app)
        if '大图标' in text: return mapped('key', app, keys=['ctrl+2'])
        if '列表' in text: return mapped('key', app, keys=['ctrl+1'])
        if '名称' in text or '排序' in text: return mapped('sort_files', app)
        return skip('平台专有查看/预览/更多菜单缺少可验证的对应步骤')
    if source == '图库':
        if '分享' in text: return skip('原分享面板依赖账号/平台；不外发内容')
        if '编辑' in text or '裁剪' in text:
            return skip('源记录未提供裁剪范围；本轮不把打开菜单计为完成编辑')
        if '首页' in text or '浏览图库' in text:
            folder = 'videos' if '视频' in text or state.get('gallery') == 'videos' else 'images'
            if '浏览图库' in text: return mapped('scroll', 'FILES', direction=1 if '下' in text else -1, directory=folder)
            return mapped('directory', 'FILES', directory=folder)
        if '放大' in text or '缩小' in text: return mapped('zoom', 'IMAGE_VIEWER', zoom_in='放大' in text)
        if '下一张图片' in text: return mapped('key', 'IMAGE_VIEWER', keys=['Right'])
        if '图片' in text: return mapped('image_open', 'IMAGE_VIEWER', index=1)
        if '下一个视频' in text: return mapped('key', 'VLC', keys=['n'])
        if '视频' in text or '播放' in text: return mapped('video_play', 'VLC')
    if source == '悟空图像':
        if '路径' in text or '“打开文件”' in text: return mapped('open_dialog', 'GIMP')
        if '.psd' in text and '打开' in text: return mapped('image_open', 'GIMP', index=int(re.search(r'\d+',text)[0]))
        if '放大' in text or '缩小' in text: return mapped('zoom', 'GIMP', zoom_in='放大' in text)
        if '切回' in text: return mapped('key', 'GIMP', keys=['alt+1'])
        if '添加图片' in text: return mapped('image_layer', 'GIMP')
        if '另存为' in text: return mapped('save_as_dialog', 'GIMP')
        if '保存' in text: return mapped('save_as_confirm', 'GIMP')
        if '窗口化' in text: return mapped('resize', 'GIMP', large=False)
        return skip('动态图像对象/动画流程与 GIMP 静态图像操作不等价')
    if source == '备忘录':
        if any(k in text for k in ['AI功能','工作总结','帮写']): return skip('AI 生成不在第一阶段；后续粘贴使用预置示例文字')
        if '新建' in text: return mapped('key', 'MOUSEPAD', keys=['ctrl+n'])
        if '粘贴' in text: return mapped('note_paste', 'MOUSEPAD')
        if '保存' in text: return mapped('note_save', 'MOUSEPAD')
        return mapped('scroll', 'MOUSEPAD', direction=1 if '下' in text else -1)
    if source == '剪映':
        if '最大化' in text: return mapped('resize', 'SHOTCUT', large=True)
        if '开始创作' in text: return mapped('key', 'SHOTCUT', keys=['ctrl+n'])
        if '导入' in text and '拖动' not in text: return mapped('open_dialog', 'SHOTCUT')
        if '打开文件' in text: return mapped('shotcut_open', 'SHOTCUT')
        if '播放' in text: return mapped('shotcut_play', 'SHOTCUT')
        if '拖动' in text: return mapped('shotcut_append', 'SHOTCUT', reason='素材栏拖放替换为将同一视频追加到时间线；累计四段')
        if '帧率' in text: return mapped('shotcut_fps', 'SHOTCUT', fps=60)
        if '按钮' in text: return mapped('shotcut_export_panel', 'SHOTCUT')
        return mapped('shotcut_export', 'SHOTCUT')
    if source == 'QQ音乐':
        if '启动' in text: return mapped('launch', 'RHYTHMBOX')
        if '热门搜索' in text: return mapped('music_play', 'RHYTHMBOX')
        if '滑动' in text: return mapped('scroll', 'RHYTHMBOX', direction=1 if '下' in text else -1)
        return skip('热门搜索、听书与播放详情依赖在线内容；本地播放单独替换')
    if source == '应用市场':
        if '启动' in text: return mapped('launch', 'SOFTWARE')
        return skip('鸿蒙游戏和应用在本地软件目录无对应结果；不伪造搜索/详情')
    return skip('没有确定对应动作；保留原记录供补充')

def build(path):
    rows = read_workbook(path)
    scopes = json.loads((ROOT/'test/configs/source20_phase1/runtime_app_scope.json').read_text())['apps']
    ids = {a['app_key']:a['app_id'] for a in scopes}
    states, previous, group_ids = {}, {}, {}
    events = []
    for row in rows:
        group = row['source_dataset_id']
        if not group: raise ValueError('Missing dataset id')
        if group not in group_ids: group_ids[group] = len(group_ids)+1
        ts = int(row['timestamp'])
        delta = (ts - previous.get(group,ts))/1000
        if delta < 0: raise ValueError('Nonmonotonic source group: '+group)
        previous[group] = ts
        event = dict(row, group_index=group_ids[group], source_delay_s=delta,
                     **classify(row, states.setdefault(group, {})))
        event['base_target_app_key'] = BASE[row['app_name']]
        event['runtime_app_id'] = ids.get(event['target_app_key'])
        event['vocab_id'] = event['runtime_app_id']-1 if event['runtime_app_id'] else 31
        event['identity_corrected'] = event['target_app_key'] != event['base_target_app_key']
        events.append(event)
    groups = [dict(index=i, source_dataset_id=g, rows=sum(e['source_dataset_id']==g for e in events)) for g,i in group_ids.items()]
    return dict(schema_version=1, source=str(path.resolve()), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                timing='compressed; original per-group deltas retained', prediction_enabled=False,
                limitations=['本地素材不等于原网页、账号、原文件或内存负载', '跳过项保留为显式边界，不用于拼接训练会话',
                             '实际执行结果与计划覆盖分开统计；输入已发送不等于内容校验通过'],
                groups=groups, counts=dict(Counter(e['disposition'] for e in events)), events=events)

def write_plan(plan, out):
    out.mkdir(parents=True, exist_ok=True)
    (out/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    fields = ['excel_row','group_index','source_dataset_id','timestamp_str','source_delay_s','app_name','event1',
              'base_target_app_key','target_app_key','runtime_app_id','vocab_id','operation','disposition','reason','params']
    with (out/'mapping.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for e in plan['events']: w.writerow(dict(e,params=json.dumps(e['params'],ensure_ascii=False)))
    lines=['# Excel 自动化映射（第一阶段）','',f"源文件 SHA-256：`{plan['source_sha256']}`",'',
           f"共 {len(plan['events'])} 条、{len(plan['groups'])} 组。计划统计：{plan['counts']}。这些不是执行成功数。",'',
           '| 原应用 | 记录数 | 目标应用 | 可执行计划 | 明确跳过 |','|---|---:|---|---:|---:|']
    for app,count in Counter(e['app_name'] for e in plan['events']).items():
        es=[e for e in plan['events'] if e['app_name']==app]
        targets=', '.join(sorted({e['target_app_key'] for e in es if e['target_app_key']})) or '—'
        n=sum(e['disposition']!='skip' for e in es)
        lines.append(f'| {app} | {count} | {targets} | {n} | {count-n} |')
    lines += ['', 'FIREFOX 是已有运行时键，实际启动 Epiphany。图库按动作分别使用 Files、Image Viewer、VLC；抖音使用浏览器本地视频页。',
              '腾讯文档使用 LibreOffice 本地文件，不验证在线协作。飞书记录全部为会议，因此不映射为聊天动作。',
              '应用中心/文件管理器中的 WPS、CAD 等按操作描述修正归属，同时保留原始 app_name。', '', '完整逐行依据见 mapping.csv；原始来源字段全部保存在 plan.json。']
    (out/'coverage.md').write_text('\n'.join(lines)+'\n')

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--xlsx',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args();plan=build(a.xlsx);write_plan(plan,a.output_dir);print(json.dumps(plan['counts']))
