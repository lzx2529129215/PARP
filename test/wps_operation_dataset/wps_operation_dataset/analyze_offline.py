"""Offline descriptive analysis; no network clients, models, or GUI execution.

Usage: PYTHONPATH=.deps python3 -m wps_operation_dataset.analyze_offline
Output publication uses DiskGuard, with a manifest written last.
"""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import statistics
from xml.sax.saxutils import escape

import pyarrow as pa
import pyarrow.parquet as pq

from .semantic_v1 import SOURCE_SHA256, SECONDARY, annotations, catalog
from .storage import DiskGuard, GiB

TIERS = {'A': 'action_recorded', 'E': 'entry_only',
         'I': 'intent_only', 'C': 'evidence_gap_or_conflict'}
INPUT = 'data/curated/gui360_office.parquet'
OUT = 'outputs/analysis_v1'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def functional(row):
    return bool((row.get('function') or '').strip())


def summarize(rows, mapping=None, candidates=None, secondary=None):
    mapping = annotations() if mapping is None else mapping
    candidates = catalog() if candidates is None else candidates
    secondary = SECONDARY if secondary is None else secondary
    grouped = defaultdict(list)
    keys = set()
    for row in rows:
        key = row['app_domain'], row['execution_id'], row['step_id']
        if key in keys:
            raise ValueError('Duplicate step: ' + str(key))
        keys.add(key)
        grouped[row['execution_id']].append(row)
    if set(grouped) != set(mapping):
        raise ValueError('Annotations must match input trajectory IDs exactly')
    if set(secondary) - set(grouped):
        raise ValueError('Unknown secondary trajectory')
    trajectories, steps, evidence = [], [], []
    for eid, group in sorted(grouped.items()):
        if len({r['app_domain'] for r in group}) != 1:
            raise ValueError('Execution ID shared across applications')
        if len({r['request'] for r in group}) != 1:
            raise ValueError('Request changes within trajectory')
        group = sorted(group, key=lambda r: r['step_id'])
        by_id = {r['step_id']: r for r in group}
        annotation = mapping[eid]
        code, tier = annotation['candidate'], annotation['tier']
        if code not in candidates or tier not in TIERS:
            raise ValueError('Unknown candidate or evidence tier')
        anchors = annotation['anchor_steps']
        if (tier == 'I') != (not anchors):
            raise ValueError('Only intent-only annotations may have no anchors')
        assignments = [(code, tier, anchors, 'primary_requested_intent')]
        assignments += [(c, 'A', ids, 'additional_observed_operation')
                        for c, ids in secondary.get(eid, [])]
        step_links = defaultdict(list)
        for op, support, ids, relationship in assignments:
            if op not in candidates or len(set(ids)) != len(ids):
                raise ValueError('Invalid candidate or repeated anchor ID')
            if any(i not in by_id or not functional(by_id[i]) for i in ids):
                raise ValueError('Anchor must reference an actual functional step')
            for i in ids:
                step_links[i].append('WPSV1.' + op)
            evidence.append({
                'execution_id': eid, 'app_domain': group[0]['app_domain'],
                'operation_id': 'WPSV1.' + op, 'relationship': relationship,
                'support': TIERS[support], 'anchor_step_ids': ids,
                'semantic_evidence': ('request_context_only' if not ids else
                    'coordinate_and_request' if not any(by_id[i]['control_text'] for i in ids)
                    else 'control_or_action_and_request'),
                'completion_verified': False,
                'anchors': [{k: by_id[i][k] for k in
                             ('step_id', 'function', 'control_text', 'control_label', 'status')}
                            for i in ids],
                'args_reference': 'Read args from source Parquet using app_domain/execution_id/step_id',
            })
        signatures = Counter((r['function'], r['control_text'], r['args'])
                             for r in group if functional(r))
        repeats = sum(n - 1 for n in signatures.values())
        active = sum(functional(r) for r in group)
        trajectories.append({
            'execution_id': eid, 'app_domain': group[0]['app_domain'],
            'request': group[0]['request'], 'primary_operation_id': 'WPSV1.' + code,
            'primary_name': candidates[code]['name'], 'family': candidates[code]['family'],
            'primary_evidence': TIERS[tier], 'raw_steps': len(group),
            'functional_steps': active, 'non_action_records': len(group) - active,
            'repeated_action_signatures': repeats,
            'anchor_step_ids': anchors, 'completion_verified': False,
            'wps_compatibility': 'unverified', 'review_note': annotation.get('note', ''),
        })
        for row in group:
            f = row['function'] or ''
            role = ('non_action_record' if not functional(row) else
                    'semantic_anchor' if row['step_id'] in step_links else
                    'selection_or_focus' if f.startswith('select_') or f == 'set_focus' else
                    'input_support' if f == 'type' else 'navigation_or_other_support')
            steps.append({k: row[k] for k in ('app_domain', 'execution_id', 'step_id',
                                               'action_type', 'function', 'status')} |
                         {'role': role, 'operation_ids': step_links[row['step_id']],
                          'primary_context': 'WPSV1.' + code,
                          'is_functional_action': functional(row)})
    app_stats = []
    for app in sorted({r['app_domain'] for r in rows}):
        ts = [t for t in trajectories if t['app_domain'] == app]
        app_stats.append(dict(app_domain=app, trajectories=len(ts),
                              raw_steps=sum(t['raw_steps'] for t in ts),
                              functional_steps=sum(t['functional_steps'] for t in ts),
                              non_action_records=sum(t['non_action_records'] for t in ts),
                              median_functional_steps=statistics.median(t['functional_steps'] for t in ts)))
    fn = Counter(r['function'] for r in rows if functional(r))
    family = Counter(t['family'] for t in trajectories)
    observed_family = defaultdict(set)
    for e in evidence:
        if e['support'] == 'action_recorded':
            observed_family[candidates[e['operation_id'].removeprefix('WPSV1.')]['family']].add(e['execution_id'])
    for code, item in candidates.items():
        eid = item['operation_id']
        ev = [e for e in evidence if e['operation_id'] == eid]
        item['primary_intent_count'] = sum(t['primary_operation_id'] == eid for t in trajectories)
        item['support_trajectory_counts'] = dict(Counter(e['support'] for e in ev))
        item['observed_applications'] = sorted({e['app_domain'] for e in ev})
        item['source_trajectory_ids'] = sorted({e['execution_id'] for e in ev})
        item['recorded_action_trajectory_count'] = len({e['execution_id'] for e in ev
                                                      if e['support'] == 'action_recorded'})
        item['sample_support'] = ('insufficient_action_support' if item['recorded_action_trajectory_count'] == 0
                                  else 'single_trajectory' if item['recorded_action_trajectory_count'] == 1
                                  else 'multiple_trajectories')
    summary = dict(
        source_sha256=SOURCE_SHA256, trajectories=len(trajectories), raw_steps=len(rows),
        functional_steps=sum(fn.values()), non_action_records=len(rows) - sum(fn.values()),
        functional_steps_with_terminal_status=sum(functional(r) and r['status'] in
                                                  ('FINISH', 'OVERALL_FINISH') for r in rows),
        no_action_trajectories=[t['execution_id'] for t in trajectories if not t['functional_steps']],
        raw_action_types=dict(Counter(r['action_type'] for r in rows)),
        functional_action_types=dict(Counter(r['action_type'] for r in rows if functional(r))),
        function_distribution=dict(fn.most_common()),
        raw_status_distribution=dict(Counter(r['status'] for r in rows)),
        non_action_status_distribution=dict(Counter(r['status'] for r in rows if not functional(r))),
        family_primary_distribution=dict(family.most_common()),
        recorded_action_family_trajectory_coverage={f: len(ids) for f, ids in sorted(
            observed_family.items(), key=lambda pair: -len(pair[1]))},
        primary_evidence_distribution=dict(Counter(t['primary_evidence'] for t in trajectories)),
        step_role_distribution=dict(Counter(s['role'] for s in steps)),
        application_distribution=app_stats, candidates=len(candidates),
        candidate_support_distribution=dict(Counter(c['sample_support'] for c in candidates.values())),
        trajectories_with_repeated_action_signatures=sum(t['repeated_action_signatures'] > 0 for t in trajectories),
        repeated_action_signatures=sum(t['repeated_action_signatures'] for t in trajectories),
        repetition_definition='Within trajectory, same function/control_text/args; candidates for review, not deduplicated operations',
        raw_step_length=dict(min=min(t['raw_steps'] for t in trajectories),
                             median=statistics.median(t['raw_steps'] for t in trajectories),
                             max=max(t['raw_steps'] for t in trajectories)),
        functional_step_length=dict(min=min(t['functional_steps'] for t in trajectories),
                             median=statistics.median(t['functional_steps'] for t in trajectories),
                             max=max(t['functional_steps'] for t in trajectories)),
        network_requests_this_analysis=0, downloaded_media_this_analysis=dict(png=0, jpg=0, mp4=0, wav=0),
        outcome_verification='not_available', annotation_method='single-pass manual semantic review, source pinned; no inter-rater accuracy estimate',
    )
    assert summary['functional_steps'] + summary['non_action_records'] == len(rows)
    assert sum(family.values()) == len(trajectories)
    return summary, trajectories, steps, evidence, candidates


def csv_bytes(records):
    f = io.StringIO(newline='')
    writer = csv.DictWriter(f, fieldnames=list(records[0]))
    writer.writeheader()
    for row in records:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                         for k, v in row.items()})
    return f.getvalue().encode('utf-8-sig')


def report(s, ts, candidates):
    lines = ['# GUI-360 Office 操作分布与 WPS Operation Space v1 候选版', '',
        '## 数据与结论', '',
        f"本地样本共 {s['trajectories']} 条 trajectory、{s['raw_steps']} 条 step 记录。"
        f"其中 {s['functional_steps']} 条有实际 function，{s['non_action_records']} 条没有 action，均为结束记录。"
        f"另外 {s['functional_steps_with_terminal_status']} 条结束状态记录含实际动作，已保留。", '',
        '这是 GUI-360 自动化任务轨迹的样本分布，不是自然用户行为日志，也不是 WPS 实测分布。'
        'success 路径、FINISH 标记与操作成功不能画等号。没有时间戳、截图和执行结果审计，无法验证任务完成率、耗时或 WSS。', '',
        '范围固定为现有三个 in_app/success 目录，存在应用数量不平衡及任务选择偏差；'
        '不能外推日常用户使用频率。in_app 任务也可能调用在线模板、图片、翻译和云历史。'
        '本轮没有网络访问、新增下载、GUI 重放、转移概率学习或模型训练。', '',
        '|应用|轨迹|原始记录|实际动作|无动作记录|实际动作数中位数/轨迹|',
        '|---|---:|---:|---:|---:|---:|']
    for a in s['application_distribution']:
        lines.append('|{app_domain}|{trajectories}|{raw_steps}|{functional_steps}|{non_action_records}|{median_functional_steps}|'.format(**a))
    lines += ['', '## 实际动作分布', '', '|function|次数|占实际动作|', '|---|---:|---:|']
    for f, n in s['function_distribution'].items():
        lines.append(f'|{f}|{n}|{n/s["functional_steps"]:.2%}|')
    lines += ['', f"原始 action_type：{s['raw_action_types']}；剔除无动作记录后：{s['functional_action_types']}。"
              '不能用原始 API 标签数量代表真正的 API 调用数。', '',
              f"每轨迹原始记录数：{s['raw_step_length']}；实际动作数：{s['functional_step_length']}。"
              f"{s['trajectories_with_repeated_action_signatures']} 条轨迹出现同 function/control_text/args 重复，"
              f"重复余次合计 {s['repeated_action_signatures']}。这些可能是重试、切换或合法重复操作，未自动去重。", '',
              '无实际动作轨迹：' + '、'.join(s['no_action_trajectories']) + '。', '',
              '## 主任务意图分布', '',
              '每条轨迹只指定一个主意图，以 request 为依据并人工核对动作。分母为 121 条轨迹；'
              '多目标任务的附加操作另存证据，不重复加入主意图分母。此处的百分比仅描述当前样本。', '',
              '|操作族|轨迹数|样本占比|', '|---|---:|---:|']
    for family, n in s['family_primary_distribution'].items():
        lines.append(f'|{family}|{n}|{n/s["trajectories"]:.2%}|')
    lines += ['', '按有动作记录的语义证据统计，操作族的轨迹覆盖如下。'
              '此处排除仅请求、入口及缺证/冲突的主标签，但保留冲突轨迹中实际观察到的其他操作。'
              '同一轨迹可覆盖多个族，因此各行不可相加作为轨迹总数；仍不代表执行成功。', '',
              '|操作族|有动作记录的轨迹覆盖|', '|---|---:|']
    for family, n in s['recorded_action_family_trajectory_coverage'].items():
        lines.append(f'|{family}|{n}|')
    lines += ['', '## 语义归并方法与候选空间', '',
        f"形成 {len(candidates)} 个候选操作，采用“操作意图 + 目标对象 + 参数”。"
        '跨应用同类操作可以共享候选 ID，但必须保留 app_domain 与目标对象；不意味着 UI 实现或内存行为相同。', '',
        '字体/字号/颜色/效果归入字符格式；表格插入、行列增删、表格样式分开；'
        '点击菜单、选区和滚动保留为动作支持，不独立认定为完成一次业务操作。'
        '模板查找与新建、表单插入与使用等以 mode 参数区分。参数字段是待实现的参数槽，'
        '本版没有自动抽取规范化参数值；原值通过 request 与原始 args 定位。', '',
        '分类不是精确的语义片段切分：证据锚点只说明某一步支持或质疑某类操作。'
        '一条轨迹可支持多个候选，每个候选按轨迹去重计覆盖；覆盖数不能相加当作独立操作次数。', '',
        '证据分为 action_recorded（有动作记录，非完成证明）、entry_only（只见入口）、'
        'intent_only（仅请求）、evidence_gap_or_conflict（请求结果缺证或动作不一致）。'
        '纯坐标拖动标记为依赖请求上下文。所有 WPS 兼容性与完成验证状态均为未验证。', '',
        '主意图证据分布：' + json.dumps(s['primary_evidence_distribution'], ensure_ascii=False) + '。', '',
        '候选支持情况：' + json.dumps(s['candidate_support_distribution'], ensure_ascii=False) + '。'
        '单轨迹候选不能视为稳定常用操作；仅请求、入口或缺证/冲突候选应先补证。'
        '本版为一次人工语义核对结果，未做双人标注一致性评估，不能报告分类准确率。', '',
        '|候选 ID|名称|主意图轨迹|有动作记录轨迹|涉及应用|', '|---|---|---:|---:|---|']
    for c in sorted(candidates.values(), key=lambda c: (-c['primary_intent_count'], c['operation_id'])):
        lines.append(f"|{c['operation_id']}|{c['name']}|{c['primary_intent_count']}|{c['recorded_action_trajectory_count']}|{', '.join(c['observed_applications'])}|")
    lines += ['', '## 存疑与边界实例', '', '|轨迹|人工核对说明|', '|---|---|']
    for t in ts:
        if t['review_note']:
            lines.append(f"|{t['execution_id']}|{t['review_note']}|")
    lines += ['', '## 后续使用边界', '',
        '可以据此挑选 WPS 操作原语、定义对象与参数、制定独立的 UI 完成条件。'
        '当前尚不能声称已构建符合真实用户频率的连续会话。需另补日常任务序列及 WPS 兼容性证据。', '',
        '样本较多覆盖格式、插入对象与页面设置；不足以代表打开多个文件、文档间切换、'
        '浏览文件管理器、持续阅读、复杂公式填充、PDF 浏览等完整用户工作流。'
        '这类缺口不能用旧 18 类操作补零后称为真实数据。', '',
        '此前 WSS 模型的 MAE 不在本轮评价范围内。操作语义归并不增加缺页特征的可辨识性，'
        '也不是 WSS 预测精度改善的证据。', '',
        '## 可复现与文件', '',
        f'输入 SHA-256：`{SOURCE_SHA256}`。原始 Parquet 未修改。', '',
        '- `trajectory_annotations.csv`：121 条主意图、证据层级、长度和人工备注。',
        '- `step_annotations.parquet`：753 条动作角色与候选锚点关联；通过三元键关联原始字段。',
        '- `operation_evidence.json`：候选与轨迹的多标签关联及精确步骤证据。',
        '- `wps_operation_space_v1_candidates.json`：候选定义、参数槽、排除边界和来源。',
        '- `summary.json` / `application_distribution.csv` / `candidate_distribution.csv` / `function_distribution.csv`：统计数据。',
        '- `distribution.svg`：主任务意图族样本分布图。',
        '- `manifest.json`：输入/产物校验和、磁盘检查、网络隔离声明。', '',
        '运行：`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.analyze_offline`。'
        '只读本地固定哈希源，输出可重复更新；拒绝未知或变化的输入，先完成全部校验再发布。', '']
    return '\n'.join(lines).encode()


def chart(summary):
    items = list(summary['family_primary_distribution'].items())
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="510" viewBox="0 0 1000 510">',
             '<rect width="1000" height="510" fill="#f8fafc"/>',
             '<g font-family="sans-serif" fill="#172554">',
             '<text x="35" y="42" font-size="23">GUI-360 Office：主任务意图分布（121 条本地轨迹）</text>',
             '<text x="35" y="74" font-size="15">当前任务样本占比，不代表真实用户使用概率；每轨迹只计一个主意图</text>']
    for i, (name, n) in enumerate(items):
        y = 110 + 48 * i
        lines += [f'<text x="35" y="{y+21}" font-size="17">{escape(name)}</text>',
                  f'<rect x="185" y="{y}" width="{n/max(v for _,v in items)*580:.1f}" height="30" rx="4" fill="#2563eb"/>',
                  f'<text x="{200+n/max(v for _,v in items)*580:.1f}" y="{y+21}" font-size="16">{n} ({n/summary["trajectories"]:.1%})</text>']
    lines += ['<text x="35" y="493" font-size="14">来源：gui360_office.parquet · 语义人工归并 v1 candidate · 未执行 WPS 回放</text>', '</g></svg>']
    return '\n'.join(lines).encode()


def run(workspace):
    guard = DiskGuard(workspace)
    before = guard.check()
    source = guard.path(INPUT)
    if digest(source) != SOURCE_SHA256:
        raise ValueError('Input SHA-256 changed; annotations require a fresh review')
    rows = pq.read_table(source).to_pylist()
    summary, trajectories, steps, evidence, candidates = summarize(rows)
    if (len(trajectories), len(steps)) != (121, 753):
        raise ValueError('Unexpected input counts')
    if any(a['trajectories'] > 2000 for a in summary['application_distribution']):
        raise ValueError('Application trajectory limit exceeded')
    artifacts = {}

    def publish(name, data):
        temporary = '.tmp/offline-' + name.replace('/', '_')
        with guard.open(temporary) as f:
            f.write(data)
        guard.commit(temporary, OUT + '/' + name)
        artifacts[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def publish_json(name, value):
        publish(name, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())

    # Invalidate the previous completion manifest before replacing any output.
    # On interruption an old manifest cannot falsely certify a mixed generation.
    manifest_path = guard.path(OUT + '/manifest.json')
    if manifest_path.exists():
        guard.check()
        manifest_path.unlink()
    publish_json('summary.json', summary)
    publish('trajectory_annotations.csv', csv_bytes(trajectories))
    publish('application_distribution.csv', csv_bytes(summary['application_distribution']))
    publish('function_distribution.csv', csv_bytes([
        dict(function=f, count=n, fraction_of_functional_steps=n/summary['functional_steps'])
        for f, n in summary['function_distribution'].items()]))
    publish('candidate_distribution.csv', csv_bytes(list(candidates.values())))
    publish_json('wps_operation_space_v1_candidates.json', dict(
        version='1.0.0-candidate', source_sha256=SOURCE_SHA256,
        annotation_scope='observed sample only; no WPS compatibility or outcome verification',
        candidates=list(candidates.values())))
    publish_json('operation_evidence.json', evidence)
    buffer = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(steps), buffer, compression='zstd')
    publish('step_annotations.parquet', buffer.getvalue().to_pybytes())
    publish('REPORT.md', report(summary, trajectories, candidates))
    publish('distribution.svg', chart(summary))
    if digest(source) != SOURCE_SHA256:
        raise ValueError('Source changed during analysis')
    after = guard.check()
    manifest = dict(completed=True, source_sha256=SOURCE_SHA256, artifacts=artifacts.copy(),
                    workspace_before=before, workspace_after_before_manifest=after,
                    no_network_by_design=True, no_training=True, no_gui_replay=True,
                    checks=dict(trajectory_count=121, step_count=753,
                                all_annotations_resolved=True, anchor_steps_valid=True,
                                source_unchanged=True, workspace_under_5_GiB=after['workspace_bytes'] <= 5*GiB,
                                free_at_least_20_GiB=after['remaining_free_bytes'] >= 20*GiB))
    publish_json('manifest.json', manifest)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    print(json.dumps(run(args.workspace), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
