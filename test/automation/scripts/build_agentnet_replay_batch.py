"""Create three isolated fixtures/scenarios; never launch an application."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from automation.wps_replay import guard, workbook_bytes, validate_spec

TASKS=[
    dict(task_id='20241125153744_3c510220-f06b-4b32-82d8-0e97f861e652',operation='recalculate_now',cell='B1',
         source_mapping=[dict(positions=[0],operation='select B1'),dict(positions=[1,2],operation='Menu/Escape and select B1'),
                         dict(positions=[3],operation='F9'),dict(positions=[4,5],operation='select A1, B1'),dict(positions=[6],operation='termination')]),
    dict(task_id='20240927235321_5855063d-3f37-47a4-ab45-5247adfdb6f7',operation='sort_ascending',cell='C2',key_column=3,
         source_mapping=[dict(positions=[0],operation='select C2'),dict(positions=[1,2,3],operation='Data → Ascending'),dict(positions=[4,5],operation='expand entire rows and confirm sort'),dict(positions=[6],operation='termination')]),
    dict(task_id='20240927234618_ace8b7f3-08d8-4296-8beb-70613b302641',operation='sort_ascending',cell='D2',key_column=4,
         source_mapping=[dict(positions=[0],operation='select D2'),dict(positions=[1,2,3],operation='Data → Ascending'),dict(positions=[4,5],operation='expand entire rows and confirm sort'),dict(positions=[6],operation='termination')]),
]

PROFILE=dict(data_tab=['Data','数据'],ascending=['Sort Ascending','Ascending','升序'],
             expand_selection=['Expand the selection','Expand selected area','扩展选定区域'],
             sort_confirm=['Sort','排序'],calibrated_on_current_gui=False)


def build(relative='outputs/executable_wps_batch_v2'):
    g=guard();g.check();out=g.path(relative)
    if out.exists():raise FileExistsError('Use a new output directory; never overwrite a used workbook')
    source=Path('/home/lzx/Desktop/AgentNet/wps_candidates/trajectories.jsonl')
    required={t['task_id'] for t in TASKS};sources={}
    with source.open() as f:
        for line in f:
            t=json.loads(line)
            if t['task_id'] in required:
                sources[t['task_id']]=dict(instruction=t.get('instruction'),steps=len(t['traj']),
                    actions=[dict(position=i,action=s['value'].get('action'),code=s['value'].get('code')) for i,s in enumerate(t['traj'])])
    if set(sources)!=required:raise ValueError('Missing source tasks')
    artifacts={};entries=[]
    def put(name,data):
        with g.open(relative+'/'+name) as f:f.write(data)
        artifacts[name]=hashlib.sha256(data).hexdigest()
    def js(name,data):put(name,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode())
    for original in TASKS:
        spec=dict(original)
        if spec['operation']=='sort_ascending':
            spec.update(headers=['row_id','payload','key_c','key_d'],rows=[
                ['r1','alpha',30,200],['r2','bravo',10,400],['r3','charlie',40,100],['r4','delta',20,300]])
        validate_spec(spec)
        prefix=spec['task_id'].split('_')[0];filename=prefix+'/'+prefix+'.xlsx';path=out/filename
        put(filename,workbook_bytes(spec))
        identity=dict(name='wps_'+prefix,app_key='WPS',title=path.name)
        action=dict(type='wps_replay_operation',path=str(path),spec=spec,profile=PROFILE,**identity)
        scenario=dict(actions=[
            dict(type='launch',command=shlex.join(['/opt/kingsoft/wps-office/office6/et',str(path)]),**identity),
            dict(type='wait_window',timeout=40,**identity),dict(type='window_state',state='maximize',**identity),
            dict(type='wait',seconds=5),action,dict(type='close',**identity)])
        js(prefix+'/scenario.json',scenario);js(prefix+'/source.json',sources[spec['task_id']]);js(prefix+'/spec.json',spec)
        entries.append(dict(task_id=spec['task_id'],scenario=str(out/prefix/'scenario.json'),fixture=str(path),
            source_steps=sources[spec['task_id']]['steps'],source_step_coverage_complete=sorted(i for x in spec['source_mapping'] for i in x['positions'])==list(range(sources[spec['task_id']]['steps'])),
            implementation_ready=True,fixture_bound=True,postcondition_implemented=True,
            runtime_status='GUI_NOT_RUN',profile_calibrated=False,
            adaptation='Original task intent/order retained; Linux navigation and synthetic workbook substituted. Setup and audit-only saves outside source-operation window; not pixel-identical replay.',
            limitations=['First GUI run must validate embedded focus, OCR labels/dialog handling and WPS recalculation behavior']))
    js('batch.json',dict(version=2,tasks=entries,new_capabilities=['standalone numeric ascending sort','NOW recalculation','cell selection','persisted workbook postconditions'],
        no_wss=True,gui_executed=False,no_new_semantic_classification=True,original_files_substituted=True))
    js('operation_space.json',dict(version='2.0.0-implemented-not-ui-validated',
        dispatcher='automation.app_automation.ACTION_HANDLERS[wps_replay_operation]',
        implementation=str(ROOT/'automation/wps_replay.py'),
        operations=[dict(id='EX_V2_SORT_ASCENDING',spec_operation='sort_ascending',
            parameters=['cell','key_column','headers','rows'],source_tasks=[x['task_id'] for x in entries if '20240927' in x['task_id']],
            completion_checks=['requested key order','whole-row association','headers','no extra cells'],
            supported_scope='numeric key in column C or D, one Data worksheet; current batch contract',profile_calibrated=False),
            dict(id='EX_V2_RECALCULATE_NOW',spec_operation='recalculate_now',parameters=['cell=B1'],
            source_tasks=[entries[0]['task_id']],completion_checks=['live displayed value advances before save','NOW formula retained','cached timestamp advances within operation window','other cells unchanged'],
            supported_scope='prepared manual-calculation NOW workbook, ISO date/time display',profile_calibrated=False)],
        setup_and_audit_outside_source_operation=True,ready_for_gui_trial=True,gui_success_not_claimed=True))
    old=g.path('outputs/executable_wps_space_v1/task_alignment.json')
    if old.exists():
        previous=json.loads(old.read_text());by_id={e['task_id']:e for e in entries}
        js('alignment_update.json',[dict(task_id=t['task_id'],previous_status=t['status'],
            previous_blockers=t['blockers'],current_status='IMPLEMENTED_GUI_NOT_RUN',
            fixture=by_id[t['task_id']]['fixture'],scenario=by_id[t['task_id']]['scenario'],
            remaining_validation=['first GUI run/profile calibration','confirm synthetic fixture/navigation adaptation'],
            end_to_end_verified=False) for t in previous if t['task_id'] in by_id])
    readme='''# AgentNet 首批可试运行自动化 v2

本次扩展 automation/app_automation.py 的 wps_replay_operation 动作，调用 automation/wps_replay.py。
已准备 NOW 重算、C 列升序、D 列升序三条源任务的独立工作簿、可运行场景和断言。未启动 WPS；GUI_NOT_RUN。

排序为独立 Data→Ascending 操作，遇到警告则扩展整行并确认；不附带筛选、复制、公式填充。
找不到或重复匹配 OCR 标签立即失败，不回退到猜测坐标。实际 UI 标签/焦点仍需首轮校准。
排序审计检查指定列升序、整行关联、表头、无额外单元格，拒绝仅排序一列、错误列和未排序结果。

NOW 素材在 B1 保存 =NOW() 和过期缓存，手动计算且禁止打开/保存时请求重算。
先在任务窗口外保存并读取基线，再等待 1.5 秒，执行原语义动作链，任务窗口外保存审计。
检查公式保留、缓存推进、时间落入 F9 操作时间范围、其他内容不变。在审计保存之前，额外复制当前 B1 的显示文本并检查时间已经推进，防止仅由保存触发重算却误判 F9 成功。
前后显示值读取是任务外校验，可能改变剪贴板；不修改工作簿。运行中的文件审计只读，绝不由 Python 伪造 GUI 结果。WPS 手动计算、日期格式和剪贴板输出仍待 GUI 验证，格式不明确时失败。

所有素材为明确声明的替代测试输入，不是 AgentNet 原始工作簿。保留完整任务意图/顺序，导航适配 Linux；不是原截图坐标级重放。
打开测试工作簿属于准备，额外保存属于审计，不计作来源任务的语义步骤。不进行 WSS 测量。

复现准备（须用新的输出目录，避免覆盖）：
  /home/lzx/Desktop/wss-ebpf-lightgbm/.venv/bin/python /home/lzx/Desktop/PARP/test/automation/scripts/build_agentnet_replay_batch.py --output outputs/executable_wps_batch_v2_new

未来获得真实运行指令后，在独立 X11/Xvfb 1920x1080 显示环境中调用现有入口：
  /home/lzx/Desktop/wss-ebpf-lightgbm/.venv/bin/python /home/lzx/Desktop/PARP/test/automation/app_automation.py <scenario.json> --display <独立显示> --trace-output <独立日志路径>

每份工作簿仅使用一次，重试重新生成独立批次。成功或失败写 replay_result.json 并保留 trace。
batch.json 表示实现和素材已具备，不表示 GUI 已成功。旧 executable_wps_space_v1 是扩展前的历史快照。
'''
    put('README.md',readme.encode())
    js('manifest.json',dict(artifacts=artifacts.copy(),source_jsonl_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        implementation_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'automation/wps_replay.py',ROOT/'automation/app_automation.py',Path(__file__)]},
        gui_executed=False,storage=g.check()))
    return entries


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',default='outputs/executable_wps_batch_v2')
    print(json.dumps(build(p.parse_args().output),ensure_ascii=False,indent=2))
