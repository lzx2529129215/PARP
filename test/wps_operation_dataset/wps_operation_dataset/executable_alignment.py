"""Static dataset-to-existing-automation alignment. Never imports GUI runners.

No 65/30 classification, no replay, and no new GUI capability implementation.
"""
import argparse
import ast
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path

from .storage import DiskGuard

OUT = 'outputs/executable_wps_space_v1'
AUTOMATION = Path('/home/lzx/Desktop/PARP/test/automation')
WSS = Path('/home/lzx/Desktop/wss-ebpf-lightgbm')
AGENTNET = Path('/home/lzx/Desktop/AgentNet')

# ID | name | actual callable | kind | historic operation | exact limits
CAPABILITIES = '''
EX01|启动隔离 WPS 实例|Session.__init__|session|0|创建 Xvfb/cgroup 且启动采集器；并非纯 GUI runner，当前须具备 systemd-user/sudo 权限
EX02|打开本地文档|UI.open|parameterized|9|path 参数；本机原生文件对话框、编辑器聚焦；PDF 入口与普通文档有差异
EX03|新建并填充 Word|UI.new|composite|6|component=word；自动写入 marker 标题及固定 25 段正文，不是纯新建空白文档
EX04|新建并填充 PPT|UI.new|composite|7|component=ppt；自动写入固定演示内容；并非任意幻灯片版式
EX05|新建并填充表格|UI.new|composite|8|component=sheet；自动写入两行测试数据，不是纯空白工作簿
EX06|选择单元格或区域|UI.cell|parameterized|16|A1 或 A1:B2；当前工作表；从 ctrl+Home 相对移动，不支持任意工作表名称
EX07|向已聚焦编辑区域粘贴文本|UI.editor_paste|parameterized|11|text 参数；正确选区与插入位置是前置条件；输入不等于公式正确计算
EX08|Word 粘贴本地 PNG|UI.picture|parameterized|11|读取 fixtures.image，经 image/png 剪贴板粘贴；不是文件菜单插图路径
EX09|保存当前文档|UI.save|parameterized|11|path=None；ctrl+s；需结合 wait_saved 和内容审计，不能仅看调用返回
EX10|另存为指定路径|UI.save|parameterized|13|path 参数；本机对话框与 Documents 相对路径；不包含格式转换选择
EX11|检查文件保存|UI.wait_saved|verification|13|path/check/timeout；默认只检查存在，具体业务正确性需要额外 check
EX12|切换两个预置 Word 文档|UI.op_10|composite|10|固定 word_a/word_b 与页签坐标；不是任意文档切换 API
EX13|Word 复制文字、粘贴图片与 HTML|UI.op_11|composite|11|固定原文首段、文末及 marker HTML；不能将整个复合动作冒充仅插图
EX14|Word 查找固定文本|UI.op_12|composite|12|查询固定为 自动化；历史审计验证对话框和查询可见，未证明正确匹配与跳转
EX15|打开五页 PPT 并放映|UI.op_14|composite|14|fixtures.ppt 五页；前后翻页再退出，OCR Section 1/5 验证
EX16|PPT 复制页、插图缩放、视频及矩形|UI.op_15|composite|15|固定素材、坐标、完整复合链；单独旋转、阴影、裁剪或替换图片未实现
EX17|表格筛选、排序、复制列与公式填充|UI.op_16|composite|16|固定 A1:L81、数值/文本筛选、B 列升序、J→N、M 列公式；不是独立任意列排序
EX18|新建工作表、复制并设字体|UI.op_17|composite|17|固定复制 A1:M81，字体 Noto Serif CJK SC、14 号；不支持任意字体或删/重排工作表
EX19|打开 PDF 并翻页|UI.op_18|composite|18|fixtures.pdf 12 页及固定 OCR 内容；不含 PDF 编辑/导出
EX20|聚焦编辑器并发送按键|UI.editor_key|primitive|0|key 参数；包括 Return、ctrl+d、F9；任意按键存在不能证明对应业务操作已实现
EX21|可见文字定位与点击|UI.click_text|primitive|0|OCR 必须找到文本；语言、页面和坐标区域受环境影响
EX22|通用低层动作分发|UI.act|primitive|0|调用现有 Adapter/engine；不是所有数据集操作都可安全映射为坐标点击
'''

# Existing dataset tasks are re-read directly. These are capability requirements,
# not an extension of the prior semantic label hierarchy.
# complete zero-based spans: start,end,capability IDs,alignment,note
TASK_REVIEW = {
 '20240927235321': [(0,5,['EX06','EX17'],'composite_mismatch','要求 C 列独立升序并扩展选区；现有 op_16 固定 B 列且额外筛选、复制、填公式'),(6,6,[],'terminal','结束标记')],
 '20240927234618': [(0,5,['EX06','EX17'],'composite_mismatch','要求 D 列独立升序；现有复合函数不能原样绑定'),(6,6,[],'terminal','结束标记')],
 '20240925010813': [(0,2,['EX05'],'composite_mismatch','要求空白工作簿；现有 new 自动写入测试数据'),(3,6,['EX18'],'missing_handler','新增、重排、删除工作表；现有只支持固定新增+复制+格式'),(7,11,['EX10'],'parameter_bindable','另存为；需恢复目标目录与文件名'),(12,12,[],'terminal','结束标记')],
 '20241001020426': [(0,0,['EX01'],'parameter_bindable','用本机启动方式替换桌面图标'),(1,3,['EX03'],'composite_mismatch','原任务空白文档，现有 new 会附加固定正文'),(4,4,['EX07'],'parameter_bindable','正文 how are you?'),(5,8,[],'missing_handler','Word 一号字体；表格 14 号字体逻辑不能代替'),(9,11,['EX09','EX10','EX11'],'parameter_bindable','首次保存及默认名称需绑定'),(12,12,[],'terminal','结束标记')],
 '20241006212825': [(0,1,['EX01'],'parameter_bindable','本机启动方式'),(2,3,['EX03'],'composite_mismatch','没有已验证的纯空白新建接口'),(4,8,['EX08'],'interaction_path_mismatch','原任务经 Insert Picture 文件对话框；现有 Word 能力使用剪贴板，且原 PNG 未绑定'),(9,12,['EX10','EX11'],'parameter_bindable','另存为 pole zero.docx，重命名可参数化'),(13,13,[],'terminal','结束标记')],
 '20241008171216': [(0,4,[],'missing_handler','替换整个演示字体；没有对应已校准实现'),(5,9,['EX10','EX11'],'parameter_bindable','保存到桌面，文件名需绑定'),(10,24,[],'external_dependency','浏览器/Zimbra/收件人/附件发送须全部保留；无当前绑定与测试账号，不能删去'),(25,25,[],'terminal','结束标记')],
 '20241129203316': [(0,0,['EX01'],'parameter_bindable','启动'),(1,5,['EX08'],'interaction_path_mismatch','Word 文件菜单插图路径未独立实现'),(6,6,['EX16'],'composite_mismatch','PPT 固定插图缩放不是 Word 图片缩放接口'),(7,9,[],'missing_handler','颜色效果和阴影'),(10,11,[],'missing_handler','按形状裁剪图片'),(12,15,[],'missing_handler','替换图片且源轨迹只有文件选择，未见完成确认'),(16,16,[],'terminal','结束标记')],
 '20241010203210': [(0,2,[],'missing_handler','演示图片阴影'),(3,11,[],'missing_handler','多个文本对象设为红色'),(12,14,['EX09'],'parameter_bindable','保存'),(15,15,['EX20'],'unverified_binding','关闭文档/窗口需处理未保存状态'),(16,19,[],'external_dependency','WeChat 文件传输与附件步骤不可省略'),(20,20,[],'terminal','结束标记')],
 '20241004224942': [(0,4,[],'missing_handler','散点图创建'),(5,11,[],'missing_handler','添加趋势线，显示公式和 R²'),(12,12,['EX16'],'composite_mismatch','PPT 图片缩放不能代替 Excel 图表缩放'),(13,13,['EX09'],'parameter_bindable','保存'),(14,14,[],'external_dependency','共享入口未绑定，不能省略'),(15,15,[],'terminal','结束标记')],
 '20241005224939': [(0,3,[],'external_dependency','浏览器课程、下载与刷新'),(4,4,['EX02'],'asset_unbound','需要原下载 Word 文件'),(5,12,[],'missing_handler','页码位置、起始值 23 及设置确认'),(13,13,[],'terminal','结束标记')],
 '20240928192332': [(0,0,['EX06'],'parameter_bindable','选择 C2'),(1,3,['EX17'],'interaction_path_mismatch','数据集用 AutoSum 菜单；现有支持文本公式填充，不是该菜单。输出区域与行列总和完成证据不足'),(4,4,[],'terminal','结束标记')],
 '20241125153744': [(0,2,['EX06','EX20'],'parameter_bindable','B1 选择、上下文菜单打开再关闭；可用 Menu/Escape 作语义等价导航替换'),(3,3,['EX20'],'parameter_bindable','F9；已有工作表重算路径'),(4,5,['EX06'],'parameter_bindable','依次选择 A1、B1'),(6,6,[],'terminal','结束标记')],
 '20241104183523': [(0,1,['EX06','EX07','EX20'],'parameter_bindable','D2 输入 =B2*C2 并确认'),(2,3,['EX06','EX20'],'parameter_bindable','选区并 ctrl+d 等价填充；原拖动终止行未绑定'),(4,4,['EX17'],'interaction_path_mismatch','AutoSum 按钮未独立实现，汇总单元格未确定'),(5,6,['EX09'],'unverified_binding','保存格式确认对话框未绑定'),(7,7,[],'terminal','结束标记')],
 '20241124215404': [(0,0,['EX06'],'parameter_bindable','选择 C4'),(1,6,[],'missing_handler','AREAS 函数向导与参数输入路径未实现；直接输入公式属于改编，且原输入字符串有歧义'),(7,7,[],'terminal','结束标记')],
}


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def read_jsonl(path):
    with Path(path).open('rb') as f:
        while True:
            line=f.readline(16*1024*1024+1)
            if not line:return
            if len(line)>16*1024*1024:raise ValueError('Input line too large')
            yield json.loads(line)


def symbols(path):
    # Parse only. Importing operation_runtime would load GUI/measurement code.
    tree=ast.parse(Path(path).read_text())
    result={}
    for node in tree.body:
        if isinstance(node,ast.ClassDef):
            for method in node.body:
                if isinstance(method,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    result[node.name+'.'+method.name]=dict(path=str(path),line=method.lineno,
                        parameters=[a.arg for a in method.args.args],
                        ast_sha256=hashlib.sha256(ast.dump(method,include_attributes=False).encode()).hexdigest())
        elif isinstance(node,ast.FunctionDef):
            result[node.name]=dict(path=str(path),line=node.lineno)
    return result


def registry_inventory(path):
    ops=json.loads(Path(path).read_text())['operations']
    result=[]
    for o in ops:
        executable=[s for s in o['steps'] if s.get('type')!='trace_marker']
        placeholder=any(s.get('status')=='not_exercised' for s in o['steps'])
        result.append(dict(operation_id=o['operation_id'],name=o['operation_name_zh'],
            status='placeholder' if placeholder or not executable else 'declared_actions_only',
            action_types=[s['type'] for s in o['steps']],required_variables=o.get('required_variables',[]),
            runtime_verified=False,source=str(path)))
    return result


def task_alignment(task,requirements):
    covered=[i for a,b,*_ in requirements for i in range(a,b+1)]
    if covered!=list(range(len(task['traj']))):raise ValueError('Task alignment must cover all source steps exactly once')
    rows=[]
    for a,b,capabilities,status,note in requirements:
        rows.append(dict(source_positions=list(range(a,b+1)),capabilities=capabilities,status=status,note=note,
            evidence=[dict(position=i,action=task['traj'][i]['value'].get('action'),code=task['traj'][i]['value'].get('code')) for i in range(a,b+1)]))
    all_bindable=all(r['status'] in ('parameter_bindable','terminal') for r in rows)
    return dict(task_id=task['task_id'],instruction=task.get('instruction'),source_steps=len(task['traj']),
        requirement_spans=rows,all_semantic_actions_bindable=all_bindable,
        status='conditional_full_action_plan' if all_bindable else 'blocked',
        fixture_bound=False,end_to_end_replay_verified=False,ready_to_replay=False,
        blockers=sorted({r['status'] for r in rows if r['status'] not in ('parameter_bindable','terminal')} |
                        {'original_fixture_not_bound','task_specific_postcondition_not_implemented'}))


def is_ready(record):
    return bool(record['all_semantic_actions_bindable'] and record['fixture_bound'] and
                record.get('task_postcondition_implemented',False) and not record['blockers'])


def run(workspace):
    g=DiskGuard(workspace);g.check()
    source_files=[AUTOMATION/'app_automation.py',AUTOMATION/'semantic/operations/wps_operations.json',
        WSS/'operation_scenarios.py',WSS/'operation_runtime.py',WSS/'wps_user_sessions.py',
        WSS/'results/wps-operation-wss-20260909-delivery/observations.jsonl',
        AGENTNET/'wps_candidates/trajectories.jsonl',AGENTNET/'wps_review/review.jsonl']
    source_hashes={str(p):sha(p) for p in source_files}
    syms=symbols(WSS/'operation_scenarios.py')|symbols(WSS/'operation_runtime.py')
    history=list(read_jsonl(WSS/'results/wps-operation-wss-20260909-delivery/observations.jsonl'))
    implementation=json.loads((WSS/'results/wps-operation-wss-20260909-v1/implementation.json').read_text())
    capabilities=[]
    for line in CAPABILITIES.strip().splitlines():
        ident,name,call,kind,op,limits=line.split('|')
        if call not in syms:raise ValueError('Missing current callable '+call)
        examples=[r for r in history if r['operation_id']==f'WPS_OP_{int(op):02}' and r.get('automation_success') is True]
        capabilities.append(dict(capability_id=ident,name=name,binding=call,kind=kind,limits=limits,
            source=syms[call],historical_ui_audit_passes=len(examples),
            historical_evidence=[dict(source_result=r['source_result'],audit=r['audit'],memory_status=r['status']) for r in examples],
            audit_scope='whole historic composite on fixed fixture; not independent proof of every extracted primitive',
            current_implementation_matches_historic_file=source_hashes[str(WSS/'operation_scenarios.py')]==implementation['operation_scenarios.py'],
            current_version_replay_verified=False))
    registry=registry_inventory(AUTOMATION/'semantic/operations/wps_operations.json')
    review={x['task_id']:x for x in read_jsonl(AGENTNET/'wps_review/review.jsonl')}
    tasks={}
    for t in read_jsonl(AGENTNET/'wps_candidates/trajectories.jsonl'):
        if review[t['task_id']]['decision']=='supported_desktop':
            old=tasks.get(t['task_id'])
            if old is None or (bool(t.get('actual_task')),len(t['traj']))>(bool(old.get('actual_task')),len(old['traj'])):tasks[t['task_id']]=t
    aligned=[];screening=[]
    for tid,t in sorted(tasks.items()):
        prefix=tid.split('_')[0]
        if prefix in TASK_REVIEW:
            aligned.append(task_alignment(t,TASK_REVIEW[prefix]))
            aligned[-1]['source_system']=review[tid]['system']
            aligned[-1]['source_trace_sha256']=hashlib.sha256(json.dumps(t['traj'],ensure_ascii=False).encode()).hexdigest()
            status=aligned[-1]['status']
        else:status='not_individually_aligned'
        screening.append(dict(task_id=tid,instruction=t.get('instruction'),status=status))
    if len(aligned)!=len(TASK_REVIEW):raise ValueError('Some reviewed tasks missing or ambiguous')
    candidate=next(t for t in aligned if t['task_id'].startswith('20241125153744'))
    plan=dict(task_id=candidate['task_id'],plan_status='blocked_on_fixture_and_validation',
        full_semantic_action_coverage=True,ready_to_replay=False,executed=False,
        equivalence='Preserves selection→context-menu open/close→F9→selection; Menu/Escape replaces platform-specific mouse navigation, not coordinate-identical replay',
        prerequisites=['Already open workbook with A1=当前时间 and B1=NOW(); formula cached numeric date/time and display format known',
            'WPS editor focus and current worksheet bound','Prepared independent test copy; original task workbook not supplied',
            'Task-specific NOW recalculation assertion; historical SUM result audit is insufficient'],
        setup_outside_task=['Session and Adapter available; UI.open on independently prepared workbook'],
        steps=[dict(source_positions=[0],binding='UI.cell',args=['B1']),
            dict(source_positions=[1],binding='UI.editor_key',args=['Menu']),
            dict(source_positions=[2],binding='UI.editor_key',args=['Escape']),
            dict(source_positions=[2],binding='UI.cell',args=['B1']),
            dict(source_positions=[3],binding='UI.editor_key',args=['F9']),
            dict(source_positions=[4],binding='UI.cell',args=['A1']),
            dict(source_positions=[5],binding='UI.cell',args=['B1'])],
        termination_source_positions=[6],
        postconditions_required_not_implemented=['B1 formula remains NOW()',
            'Recalculated cached value corresponds to current time within predeclared tolerance',
            'Document content outside recalculation unchanged'],
        do_not_substitute='Do not replace NOW with SUM merely because SUM was previously tested')
    fixture_paths=[Path('/home/lzx/Desktop/PARP/test/samples/wps')/name for name in
        ['word_0040_fixture.docx','spreadsheet_0060.xlsx','document_0070.pdf','video_5s_test.mp4']]+[
        WSS/'fixtures/wps_operations_v1_image.png',WSS/'fixtures/briefing_small.pptx']
    fixtures=[dict(path=str(p),exists=p.is_file(),bytes=p.stat().st_size if p.is_file() else None,
                   sha256=sha(p) if p.is_file() else None,role='existing synthetic test asset; not original AgentNet task file') for p in fixture_paths]
    session_evidence=[]
    for f in sorted((WSS/'results/user-session-pilot-v2').glob('*/result.json')):
        r=json.loads(f.read_text());session_evidence.append(dict(path=str(f),sha256=sha(f),session=r['session'],
            status=r['status'],audits=r['audits'],source_task_ids=r['source_task_ids'],
            interpretation='intent-adapted session only; not full replay of listed source tasks'))
    summary=dict(capabilities=len(capabilities),registry_entries=len(registry),registry_placeholders=sum(r['status']=='placeholder' for r in registry),
        selected_agentnet_tasks=len(tasks),individually_aligned_tasks=len(aligned),
        not_individually_aligned_tasks=len(tasks)-len(aligned),
        conditional_full_action_plans=sum(t['all_semantic_actions_bindable'] for t in aligned),
        ready_to_replay_tasks=sum(is_ready(t) for t in aligned),end_to_end_verified_agentnet_tasks=0,
        historical_ui_verified_operation_classes=sorted({r['operation_id'] for r in history if r.get('automation_success')}),
        current_procedure_file_changed_since_historical_run=True,
        no_gui_started=True,no_remote_access=True,no_65_30_reclassification=True,
        conclusion='No unconditionally ready full AgentNet replay is established; one complete semantic action plan is conditional, other reviewed tasks have explicit gaps. Unreviewed tasks are not classified as impossible.')
    previous=g.path(OUT+'/manifest.json')
    if previous.exists():g.check();previous.unlink()
    outputs={}
    def put(name,data):
        with g.open('.tmp/executable-align-'+name) as f:f.write(data)
        g.commit('.tmp/executable-align-'+name,OUT+'/'+name);outputs[name]=sha(g.path(OUT+'/'+name))
    def js(name,obj):put(name,(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode())
    js('operation_space.json',dict(version='1.0.0-static',capabilities=capabilities,
        environment='Historical WPS 11.1.0.11723.XA, Linux Xvfb 1920x1080 zh_CN; not revalidated this turn',
        rule='Existing generic key/click primitives never imply arbitrary business capability'))
    js('registry_audit.json',registry);js('task_alignment.json',aligned);js('task_screening.json',screening)
    js('first_batch_conditional_plan.json',plan);js('ready_to_replay.json',[t for t in aligned if is_ready(t)])
    js('fixtures_inventory.json',fixtures);js('historical_adapted_sessions.json',session_evidence);js('summary.json',summary)
    f=io.StringIO(newline='');writer=csv.DictWriter(f,fieldnames=['capability_id','name','binding','kind','historical_ui_audit_passes','limits']);writer.writeheader()
    writer.writerows({k:c[k] for k in writer.fieldnames} for c in capabilities);put('capability_matrix.csv',f.getvalue().encode('utf-8-sig'))
    lines=['# Executable WPS Operation Space：现有能力对齐', '',
        '本轮只做静态代码/素材/历史证据对齐。没有启动 WPS、执行数据集 code、下载数据或修改 65/30 分类。', '',
        '## ① 目前哪些操作已有自动化能力', '',
        f"识别出 {len(capabilities)} 项具体调用能力，其中含参数化接口、固定复合流程、低层原语和校验工具，不能视作 {len(capabilities)} 类均已验证的独立业务操作。", '',
        '历史 18 类流程中 06～18 共 13 类各有 5 次 UI 审计通过记录。部分记录的 PARTIAL 是内存扫描覆盖不完整，不是 GUI 操作失败。'
        '当前 operation_scenarios.py 与历史 implementation.json 的文件哈希不同，故只称历史证据，不冒称当前版本已重新验证。', '',
        '|能力|当前调用|范围和限制|', '|---|---|---|']
    for c in capabilities:lines.append(f"|{c['capability_id']} {c['name']}|{c['binding']}|{c['limits']}|")
    lines+=['',f"另核对语义注册表 {len(registry)} 个条目：{summary['registry_placeholders']} 个包含占位/未执行标记。"
        '其余也只是动作声明，不因名称叫“插图”“另存为”就自动认定实现完整。实际插图能力应引用 UI.picture/op_15，而不是注册表占位。', '',
        '不能由现有证据推导的能力包括：Word/PPT 任意字体颜色、图片阴影/裁剪/替换、页码设置、图表趋势线、'
        '通用独立排序和 AutoSum/函数向导、邮件/WeChat 完整发送链。部分可由新代码实现，但那属于补能力，不属于“现在已经有”。', '',
        '## ② 哪些 AgentNet 任务现在能完整重放', '',
        f"已有 305 个候选任务中，本轮对 {len(aligned)} 条优先任务逐步骤核对；其余 {len(tasks)-len(aligned)} 条明确标为尚未逐条对齐，不判为不可能。", '',
        '**当前能无条件列入完整重放清单的任务：0 条；有完整语义动作绑定、但素材和验收仍未落实的候选：1 条。**'
        '这里的 ready 要求所有操作有绑定、输入素材已落实、任务级后置检查已实现。它与“历史曾完整重放成功”是两个不同状态。'
        '本轮不运行 WPS 不影响静态 ready 判定；当前阻塞是具体输入、绑定或检查缺失，而非单纯没有新运行。', '',
        '|源任务|结论|主要缺口|','|---|---|---|']
    for t in aligned:
        reasons=[r['note'] for r in t['requirement_spans'] if r['status'] not in ('parameter_bindable','terminal')]
        lines.append(f"|{t['task_id']}|{t['status']}|{'; '.join(reasons) if reasons else '动作可绑定；NOW 初始工作簿与结果断言待落实'}|")
    lines+=['','## 第一批候选：NOW 重算任务', '',
        f"任务 `{candidate['task_id']}`，保留完整语义操作：", '',
        '`选择 B1 → 打开/关闭上下文菜单 → F9 重算 → 选择 A1 → 返回 B1`。', '',
        '已有 UI.cell/UI.editor_key 可以表达整条链，调用和 source step 对应关系见 first_batch_conditional_plan.json。'
        'Menu/Escape 是跨平台菜单导航替换，不是原始坐标照搬。当前候选不标 ready：缺少已绑定的 NOW 工作簿、'
        '时间缓存刷新断言；现有 SUM 测试通过不能证明 NOW 时间行为。没有生成新 GUI handler 或擅自把 NOW 换成 SUM。', '',
        '## 为什么以前的成功会话不能当作原任务完整重放', '',
        'user-session-pilot-v2 的 Word、表格会话有 UI_AUDIT_PASS，但原代码明确按 intent 改编。'
        'Word 会话打开已有报告、追加固定正文/图片和 PDF 阅读，没有实现源任务的一号字体设置；'
        '表格会话用 =SUM(A2:B2)、O2:O81 填充和改 A2，不是原 AutoSum 菜单链或 NOW 任务。'
        'PPT 源任务涉及换字体、发送邮件，当前连续会话替换为复制页/放映/PDF，不能算完整覆盖。', '',
        '“完整”不允许删掉外部步骤、使用包含额外业务操作的 op_16 冒充单独排序，或把占位 marker 当实现。'
        '纯新建与 new 自动灌入测试文字不同；图片文件插入与剪贴板插入对 WSS 也可能不同。'
        '若后续允许目标等价改编，需另列适配差异和新的验收条件，不混入原流程完整重放清单。', '',
        '## 交付与复现', '',
        '- operation_space.json / capability_matrix.csv：调用位置、参数限制与历史 UI 审计来源。',
        '- registry_audit.json：真实动作声明与占位清单。',
        '- task_alignment.json：14 条任务所有 source step 的能力映射与阻塞原因。',
        '- task_screening.json：305 条任务的对齐状态，未审阅不冒充不支持。',
        '- first_batch_conditional_plan.json：首个条件候选的完整绑定计划；ready_to_replay.json 当前为空。',
        '- fixtures_inventory.json / historical_adapted_sessions.json：素材和历史改编结果边界。', '',
        '`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.executable_alignment`。'
        '仅 ast.parse GUI 源码，不导入或调用 GUI runner。DiskGuard 保护所有产物写入，manifest 最后发布。', '']
    put('REPORT.md','\n'.join(lines).encode())
    if any(sha(p)!=h for p,h in source_hashes.items()):raise ValueError('Alignment source changed during inspection')
    result=dict(completed=True,artifacts=outputs.copy(),source_hashes=source_hashes,storage=g.check(),summary=summary)
    js('manifest.json',result)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',default=str(Path(__file__).resolve().parents[1]))
    print(json.dumps(run(parser.parse_args().workspace),ensure_ascii=False,indent=2))
