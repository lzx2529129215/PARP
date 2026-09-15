"""Two-level, offline operation vocabulary. Mechanism hypotheses, not WSS clusters."""
import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .analyze_offline import csv_bytes, digest
from .storage import DiskGuard

SOURCE = 'outputs/analysis_v1'
OUT = 'outputs/wss_operation_space_v1'

# Stable ID | name | fine labels | mechanism hypothesis | discriminating parameters
DEFINITIONS = '''
WT01|界面配置|UI_CUSTOMIZE UI_THEME DESIGN_MODE|界面配置与控件资源更新|setting,mode,theme,enabled
WT02|视图与窗格|VIEW_LAYOUT VIEW_GUIDES VIEW_PANE|可见区域、布局显示与窗格资源更新|view,pane,visible_pages,zoom,enabled
WT03|元数据与引用编辑|DOC_PROPERTIES NAMED_RANGE OBJECT_ALT_TEXT HYPERLINK|修改属性或引用结构，可能触发局部 UI 更新|target,mode,range_size,metadata_size
WT04|评论与修订状态|TRACK_CHANGES COMMENT_EDIT|审阅状态或批注结构更新，可能影响正文渲染|mode,scope,comment_count,revision_count
WT05|文本内容修改|TEXT_INSERT TEXT_DELETE SYMBOL|文本缓冲、撤销记录与受影响区域重新排版|target,mode,character_count,selection_size
WT06|字符与段落格式|TEXT_FORMAT PARAGRAPH_FORMAT|样式更新及局部或多段落重新排版|scope,character_count,paragraph_count,font,style
WT07|页面几何布局|PAGE_LAYOUT|页面尺寸或分栏改变引起分页与布局重算|app,scope,page_count,columns,orientation,size
WT08|页面装饰与重复内容|PAGE_DECORATION HEADER_FOOTER PAGE_NUMBER SLIDE_THEME|页级样式、重复页内容或演示主题更新|scope,page_count,object_count,theme,mode
WT09|文档范围扫描与更新|DOC_INSPECT DOC_STATISTICS DOC_COMPAT_CHECK TOC_UPDATE FIND_REPLACE|遍历文档范围及可选结果写回|scope,document_size,query,write_back,match_count
WT10|打印预览|PRINT_PREVIEW|分页及打印渲染路径|page_count,preview_range,print_settings
WT11|文档序列化|DOC_SAVE DOC_SAVE_AS DOC_CONVERT|文档编码、格式转换与文件写出|mode,source_format,target_format,document_size,media_bytes
WT12|文档保护|DOC_PROTECT|保护状态更新或加解密相关路径，依 mode 区分|protection_type,enabled,document_size
WT13|模板查找与实例化|DOC_TEMPLATE|模板目录或资源加载及可选文档构建|mode,source,template_size,resource_bytes
WT14|云端版本入口|DOC_HISTORY|云服务界面、认证与版本列表相关路径|provider,mode,network_state,version_count
WT15|表格结构修改|TABLE_INSERT TABLE_STRUCTURE|表格节点分配、增删与布局更新|app,mode,rows,columns,affected_cells
WT16|表格样式更新|TABLE_FORMAT|单元格或表格样式和边框渲染|affected_cells,style,border
WT17|区域数据变换|DATA_SORT DATA_DEDUP|选区扫描、比较与数据重排|target,rows,columns,data_type,mode
WT18|外部数据与查询|DATA_QUERY DATA_IMPORT|查询引擎或外部数据解析和加载|mode,source,format,input_bytes,rows,columns
WT19|二维对象编辑|TEXTBOX WORDART SHAPE_INSERT SHAPE_FORMAT OBJECT_TRANSFORM|对象树修改及二维几何或绘制更新|target,mode,object_count,geometry,source_pixels
WT20|结构化图示构建|SMARTART CHART_INSERT|根据结构或数据构建图示布局|engine,layout,node_count,series_count,data_points
WT21|图像与背景处理|IMAGE_INSERT IMAGE_APPEARANCE SLIDE_BACKGROUND|图像资源读取、解码、效果或背景渲染|mode,source,encoded_bytes,width,height,effect
WT22|三维资源插入|MODEL_3D|三维模型加载与渲染资源准备|source,model_bytes,texture_bytes,mesh_count
WT23|翻译|TRANSLATE|语言服务入口、文本提交与可能的译文写回|mode,language,character_count,network_state
WT24|拼写检查|SPELLCHECK|词典或校对模块及文本扫描|language,scope,character_count
WT25|朗读|READ_ALOUD|语音服务或合成模块及播放缓冲|language,character_count,mode,engine
WT26|宏录制入口|MACRO_RECORD|宏子系统及录制配置入口|mode,engine,event_count
WT27|幻灯片结构编辑|SLIDE_REORDER SLIDE_SECTION SLIDE_INSERT|幻灯片树、引用和缩略图更新|mode,slide_count,affected_slides,embedded_objects
WT28|放映与动画配置|SLIDESHOW_SETTINGS SLIDE_TIMING ANIMATION_REORDER|放映或动画元数据更新，不代表播放|mode,affected_slides,effect_count,timing
WT29|公式对象构建|EQUATION|数学结构编辑器及公式排版|structure,node_count,engine
WT30|预定义部件与控件|QUICK_PART FORM_CONTROL|部件或表单对象实例化及控件交互|target,mode,part_size,control_count
'''

GLOBAL_CONTEXT = {
    'available_now': ['app_domain', 'fine_operation_id', 'evidence_support', 'anchor_step_ids'],
    'required_for_future_measurement': [
        'WPS_version', 'document_id', 'document_size', 'page_slide_sheet_count',
        'open_document_count', 'object_and_media_sizes', 'selection_scope',
        'cold_or_warm_start', 'module_load_state', 'network_or_local_source',
        'background_activity', 'target_process_identity', 'measurement_window_definition'],
    'future_pre_operation_features_only': [
        'document_state_before', 'cache_state_before', 'planned_parameters',
        'previous_observed_operations', 'planned_measurement_duration'],
    'not_available_in_current_data': [
        'measured_WSS', 'operation_start_end_time', 'actual_duration',
        'process_memory_state', 'verified_WPS_completion'],
    'leakage_rule': '预测操作前 WSS 时不可输入该操作结束后的内存、实际耗时、扫描结果或未来步骤。'
                    '已知结束窗口的回顾性估计需另设任务定义。',
}


def vocabulary(fine):
    tokens, mapping = [], {}
    for line in DEFINITIONS.strip().splitlines():
        token, name, labels, rationale, params = line.split('|')
        members = ['WPSV1.' + x for x in labels.split()]
        for member in members:
            if member in mapping:
                raise ValueError('Fine label mapped more than once: ' + member)
            mapping[member] = token
        tokens.append(dict(token_id=token, name=name, fine_operation_ids=members,
                           mechanism_hypothesis=rationale, distinguishing_parameters=params.split(','),
                           empirical_WSS_validation=False))
    if set(mapping) != {x['operation_id'] for x in fine}:
        raise ValueError('Mapping must cover exactly the fine catalog')
    if len({t['token_id'] for t in tokens}) != len(tokens):
        raise ValueError('Duplicate token ID')
    if not 20 <= len(tokens) <= 30:
        raise ValueError('Expected 20–30 upper-level tokens')
    return tokens, mapping


def project_evidence(evidence, mapping):
    # Preserve weak/conflicting support; no conversion to successful events.
    return [dict(e, wss_token_id=mapping[e['operation_id']],
                 event_boundary_verified=False, wss_training_ready=False) for e in evidence]


def run(workspace):
    g = DiskGuard(workspace)
    g.check()
    manifest = json.loads(g.path(SOURCE + '/manifest.json').read_text())
    if not manifest['completed']:
        raise ValueError('Fine analysis incomplete')
    for name, info in manifest['artifacts'].items():
        if digest(g.path(SOURCE + '/' + name)) != info['sha256']:
            raise ValueError('Fine analysis artifact changed: ' + name)
    source_file = g.path('data/curated/gui360_office.parquet')
    if digest(source_file) != manifest['source_sha256']:
        raise ValueError('Original source changed')
    fine = json.loads(g.path(SOURCE + '/wps_operation_space_v1_candidates.json').read_text())['candidates']
    tokens, mapping = vocabulary(fine)
    evidence = project_evidence(json.loads(g.path(SOURCE + '/operation_evidence.json').read_text()), mapping)
    with g.path(SOURCE + '/trajectory_annotations.csv').open(encoding='utf-8-sig', newline='') as f:
        trajectories = [dict(r, primary_intent_wss_token=mapping[r['primary_operation_id']]) for r in csv.DictReader(f)]
    steps = pq.read_table(g.path(SOURCE + '/step_annotations.parquet')).to_pylist()
    for step in steps:
        step['wss_token_ids'] = sorted({mapping[x] for x in step['operation_ids']})
        step['primary_intent_wss_token'] = mapping[step['primary_context']]
        step['event_boundary_verified'] = False
    distribution = []
    for token in tokens:
        token_id = token['token_id']
        ev = [e for e in evidence if e['wss_token_id'] == token_id]
        distribution.append(dict(token_id=token_id, name=token['name'], fine_label_count=len(token['fine_operation_ids']),
            primary_intent_trajectories=sum(t['primary_intent_wss_token'] == token_id for t in trajectories),
            action_recorded_trajectory_coverage=len({e['execution_id'] for e in ev if e['support'] == 'action_recorded'}),
            entry_only_trajectory_coverage=len({e['execution_id'] for e in ev if e['support'] == 'entry_only'}),
            intent_only_trajectory_coverage=len({e['execution_id'] for e in ev if e['support'] == 'intent_only'}),
            gap_or_conflict_trajectory_coverage=len({e['execution_id'] for e in ev if e['support'] == 'evidence_gap_or_conflict'})))
    assert len(fine) == 65 and len(trajectories) == 121 and len(steps) == 753
    assert sum(d['primary_intent_trajectories'] for d in distribution) == 121
    artifacts = {}
    previous = g.path(OUT + '/manifest.json')
    if previous.exists():
        g.check()
        previous.unlink()

    def publish(name, data):
        temporary = '.tmp/tokens-' + name
        with g.open(temporary) as f:
            f.write(data)
        g.commit(temporary, OUT + '/' + name)
        artifacts[name] = digest(g.path(OUT + '/' + name))

    def js(name, data):
        publish(name, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())

    js('operation_space.json', dict(version='1.0.0-candidate', fine_layer=fine,
        upper_layer=tokens, fine_to_token=mapping, context_schema=GLOBAL_CONTEXT,
        design_status='mechanism hypotheses; no measured WSS clustering',
        unmapped_operation_policy='reject; never silently map unseen operations to an existing token'))
    publish('fine_to_token.csv', csv_bytes([dict(fine_operation_id=c['operation_id'], fine_name=c['name'],
        wss_token_id=mapping[c['operation_id']]) for c in fine]))
    js('token_evidence.json', evidence)
    publish('trajectory_annotations.csv', csv_bytes(trajectories))
    publish('token_distribution.csv', csv_bytes(distribution))
    buffer = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(steps), buffer, compression='zstd')
    publish('step_annotations.parquet', buffer.getvalue().to_pybytes())
    lines = ['# WPS 两层操作空间 v1 候选版', '',
        '**65 个细粒度语义标签全部保留，上层映射为 30 个 WSS-oriented token。**'
        '每个细标签只有一个固定父 token；每次观测同时保留细标签、应用、参数和证据等级。', '',
        '这是一套按潜在处理机制设计的假设空间，不是根据 WSS 实测聚类的结果。'
        '当前数据不足以证明同 token 的操作工作集接近，也不能为 token 指定固定 MiB。'
        '选择 30 个是为了保留打印、图像、三维、查询、公式和语言服务等不同路径，不强行压到 20 个。', '',
        '## 上层词表与归并依据', '',
        '|Token|名称|细标签（省略 WPSV1.）|处理机制假设|', '|---|---|---|---|']
    for t in tokens:
        lines.append(f"|{t['token_id']}|{t['name']}|{', '.join(x.removeprefix('WPSV1.') for x in t['fine_operation_ids'])}|{t['mechanism_hypothesis']}|")
    lines += ['', '## 必须保留的差异', '',
        '- 同一 token 不表示同样的 WSS。输入表示应为 `(token, fine_label, app, parameters, pre_operation_context)`，不能只留下 token ID。',
        '- 文本修改和格式更新分开；分页几何变更与页面装饰分开；打印预览独立于普通视图；表格结构、样式和区域数据变换分开。',
        '- 保存与转换共享序列化父类，但保留格式和模式；保护独立，因为只读标志与密码处理路径不同，仍须细标签参数区分。',
        '- 图片插入/效果/图片背景共享图像父类，保留解码、效果、来源与像素规模；三维独立。对象旋转/尺寸归二维对象编辑，但图片目标必须保留 source_pixels，实测若分离再修订父类。',
        '- SmartArt 和图表暂归结构化图示，保留 engine、节点数和数据点数；公式编辑器单列。部件与控件也保留实例化/交互 mode。',
        '- 模板查找与创建、云历史入口与真正拉取、查询入口与执行绝不可仅凭父 token 混为等价执行。细标签、mode 和证据等级是强制保留项；未知参数为 null，不填零。',
        '- WT28 仅代表配置，不代表真正放映；WT26 只有宏入口证据，不能称为宏执行。', '',
        '## 当前样本覆盖', '',
        '主意图列每条轨迹仅计一次，总和为 121；动作覆盖列按 token 内轨迹去重。'
        '一条轨迹可以覆盖多个 token，各 token 覆盖数不能相加当作独立操作次数。', '',
        '|Token|细标签数|主意图轨迹|有动作记录的轨迹覆盖|', '|---|---:|---:|---:|']
    for d in distribution:
        lines.append(f"|{d['token_id']}|{d['fine_label_count']}|{d['primary_intent_trajectories']}|{d['action_recorded_trajectory_coverage']}|")
    lines += ['', '## 序列建模接口与防止错误标签', '',
        '`token_evidence.json` 保留每条细粒度证据的父 token、支持等级和锚点。'
        '`step_annotations.parquet` 增加锚点 token 列表，同时保留原细标签；无动作步骤的列表仍为空。'
        'primary_intent_wss_token 是请求上下文，不是该步骤已经执行此操作。', '',
        '没有将按执行 ID 排序的任务拼成用户会话，也没有把每次点击扩展为一个语义 token 事件。'
        '同一轨迹的多个标签可能属于组合操作，锚点不是可靠的操作起止边界。'
        '所有证据均标记 event_boundary_verified=false、wss_training_ready=false；未来补齐边界和 WSS 标签后再构造序列。', '',
        '未来真实事件至少包含 session_id、operation_instance_id、token_id、fine_label、app、'
        'planned_parameters、pre_operation_context、start/end、evidence_quality、WSS_measurement。'
        '开始前预测时只能用当时可得参数与历史；操作后实际耗时、WSS、扫描结果不能泄漏到输入。', '',
        '工作集的定义必须固定观察窗口、进程集合与采集口径；相同操作在不同文档大小、缓存冷热、'
        '模块首次加载、多文档驻留、选择范围及后台活动下可能相差很大。参数槽与上下文定义见 operation_space.json。', '',
        '## 后续验证与修订准则（本轮未执行）', '',
        '采集同一 token 下不同细标签在相同应用、文档规模、作用范围、冷热状态及窗口口径下的重复实测。'
        '比较条件化 WSS 分布和组内离散程度；若细标签在控制上下文后仍有稳定差异，拆分该 token。'
        '跨 token 只有在机制和实测响应都相近时才考虑合并。不要因为样本稀少而合并。', '',
        '未来比较细标签模型、仅 token 模型、token+细标签+上下文模型，以会话/文档分组划分训练测试，'
        '报告 MAE/RMSE 和分规模误差；阈值应按实验目标预先设定。当前不训练模型、不学习转移概率、不执行 WSS 重放。', '',
        '当前 65 标签没有覆盖的打开多文档、文档切换、持续阅读、实际放映、复杂公式计算等应标记为词表外，'
        '待有证据后版本化扩展，不能硬塞进现有 token。', '',
        '## 复现', '',
        '`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.wss_tokens_v1`。'
        '完全离线、DiskGuard 保护写入，不修改原始数据或 analysis_v1。未知标签拒绝投影。'
        '中断时不发布完成 manifest；残留 .tmp/tokens-* 需检查移走后重跑。', '']
    publish('REPORT.md', '\n'.join(lines).encode())
    if digest(source_file) != manifest['source_sha256']:
        raise ValueError('Source changed while projecting')
    result = dict(completed=True, fine_labels=65, tokens=len(tokens), trajectories=len(trajectories),
                  steps=len(steps), evidence_records=len(evidence), artifacts=artifacts.copy(),
                  source_sha256=manifest['source_sha256'], fine_manifest_sha256=digest(g.path(SOURCE+'/manifest.json')),
                  storage_before_manifest=g.check(), no_training=True, no_replay=True, no_remote_access_by_design=True)
    js('manifest.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default=str(Path(__file__).resolve().parents[1]))
    print(json.dumps(run(parser.parse_args().workspace), ensure_ascii=False, indent=2))
