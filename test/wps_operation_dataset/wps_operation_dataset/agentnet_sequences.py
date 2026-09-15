"""Local AgentNet semantic sequence exploration, with explicit unknown barriers.

Rules produce provisional candidates. Manually reviewed complete-task spans are
reported separately. No code from the dataset is executed; no network is used.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

from .analyze_offline import csv_bytes, digest
from .semantic_v1 import catalog
from .storage import DiskGuard
from .wss_tokens_v1 import vocabulary

OUT = 'outputs/agentnet_sequences_v1'

# Complete, zero-based inclusive action-position spans, reviewed from action+code.
# These do not claim successful execution or exact timing. OOV labels are NOT
# additions to the frozen 65/30 vocabulary. TERMINAL is excluded from sequences.
# TASK prefixes uniquely resolved against this pinned local extraction.
MANUAL = {
    '20240927235321': [(0,5,'DATA_SORT','action_recorded'), (6,6,'TERMINAL','marker')],
    '20240925010813': [(0,2,'OOV:DOC_NEW','action_recorded'),
        (3,6,'OOV:SHEET_STRUCTURE','action_recorded'), (7,11,'DOC_SAVE','action_recorded'), (12,12,'TERMINAL','marker')],
    '20241001020426': [(0,0,'OOV:APP_LAUNCH','action_recorded'), (1,3,'OOV:DOC_NEW','action_recorded'),
        (4,4,'TEXT_INSERT','action_recorded'), (5,8,'TEXT_FORMAT','action_recorded'),
        (9,11,'DOC_SAVE','action_recorded'), (12,12,'TERMINAL','marker')],
    '20241004224942': [(0,4,'CHART_INSERT','action_recorded'), (5,11,'OOV:CHART_ANALYSIS','action_recorded'),
        (12,12,'OBJECT_TRANSFORM','action_recorded'), (13,13,'DOC_SAVE','action_recorded'),
        (14,14,'OOV:SHARE','entry_only'), (15,15,'TERMINAL','marker')],
    '20241005224939': [(0,3,'OOV:EXTERNAL','action_recorded'), (4,4,'OOV:DOC_OPEN','action_recorded'),
        (5,12,'PAGE_NUMBER','action_recorded'), (13,13,'TERMINAL','marker')],
    '20241006212825': [(0,1,'OOV:APP_LAUNCH','action_recorded'), (2,3,'OOV:DOC_NEW','action_recorded'),
        (4,8,'IMAGE_INSERT','action_recorded'), (9,12,'DOC_SAVE','action_recorded'), (13,13,'TERMINAL','marker')],
    '20241008171216': [(0,0,'OOV:VIEW_NAVIGATION','action_recorded'), (1,4,'TEXT_FORMAT','action_recorded'),
        (5,9,'DOC_SAVE_AS','action_recorded'), (10,24,'OOV:EXTERNAL','action_recorded'), (25,25,'TERMINAL','marker')],
    '20241129203316': [(0,0,'OOV:APP_LAUNCH','action_recorded'), (1,5,'IMAGE_INSERT','action_recorded'),
        (6,6,'OBJECT_TRANSFORM','action_recorded'), (7,9,'OOV:IMAGE_EFFECT','action_recorded'),
        (10,11,'OOV:IMAGE_CROP','action_recorded'), (12,15,'OOV:IMAGE_REPLACE','entry_only'), (16,16,'TERMINAL','marker')],
    '20241010203210': [(0,2,'OOV:IMAGE_EFFECT','action_recorded'), (3,11,'TEXT_FORMAT','action_recorded'),
        (12,14,'DOC_SAVE','action_recorded'), (15,15,'OOV:DOC_CLOSE','action_recorded'),
        (16,19,'OOV:EXTERNAL','action_recorded'), (20,20,'TERMINAL','marker')],
}

MANUAL_TRACE_HASHES = {
    '20240927235321': '3866dac317d8507130f546a36c50001b3150f5c6912cf6a7d7149a45a5bad0aa',
    '20240925010813': '71c10e01ea3644ef34c1ad9df5007bff07a8257d63ccc1dfab82a3850720ead5',
    '20241001020426': '73283b236675109ff48e205454a831538ee1f3a9bafe116ade89a40f7d21bb63',
    '20241004224942': '81829bf76515ecf48f0cdf93367897c934f1796cfd326260c090b6add4c742a2',
    '20241005224939': 'c2d1cdd9523c6e4c6e17bb0914fbe9bd1d399220808a697bedbe04d46322b86b',
    '20241006212825': '97c5bd9a84768300cf5388afb976c6429ac2c36fd87884ee19db70802f0228dc',
    '20241008171216': 'b5b03a42e73778d6028a44eccb8be8a5d17d8f522cafb84265b3cf8790edbb60',
    '20241129203316': 'ffb732b222c28551eee604f3a90dedb974a7abaa4c3c2dd04b8bc67d8f6bfc90',
    '20241010203210': '4a28327f34ea65105481cc75c31208f04be7a234cc254886eeab6c34046e2425',
}

# Match executed action head, not an instruction/observation or trailing rationale.
# Fine labels absent from these conservative rules stay OOV:UNKNOWN.
RULES = [
    ('OOV:IMAGE_REPLACE', r'replace (?:picture|image)'),
    ('OOV:IMAGE_CROP', r'\bcrop(?:ping)?\b'),
    ('OOV:IMAGE_EFFECT', r'\bshadow\b|picture effects|image effects'),
    ('OOV:CHART_ANALYSIS', r'trendline|r-squared|r平方'),
    ('OOV:SHEET_STRUCTURE', r'(?:new|delete|rename|rearrange|move|drag).{0,45}(?:worksheet|sheet\d|sheet tab)|(?:sheet\d|sheet tab).{0,35}(?:drag|delete|rename)'),
    ('OOV:FORMULA_CALC', r'formula|recalculate|insert function|autosum|\b(?:SUMIF|VLOOKUP|HLOOKUP|SUMIFS|IFERROR|NOW|MMULT)\b'),
    ('OOV:DATA_FILTER', r'\bfilter(?:ing)?\b'),
    ('OOV:SHARE', r'\bshare\b|分享'),
    ('OOV:DOC_CLOSE', r'close.{0,35}(?:document|workbook|presentation|wps|application)'),
    ('OOV:DOC_NEW', r'(?:new|blank).{0,35}(?:document|spreadsheet|workbook|presentation)|(?:document|spreadsheet).{0,35}creat'),
    ('DOC_SAVE_AS', r'save as|另存为'),
    ('DOC_CONVERT', r'export.{0,40}(?:pdf|document)|convert.{0,40}(?:pdf|docx|xlsx|csv)'),
    ('DOC_SAVE', r'\bsave\b|保存|ctrl\+s|command\+s'),
    ('PAGE_NUMBER', r'page number|页码'),
    ('HEADER_FOOTER', r'header|footer|页眉|页脚'),
    ('PAGE_LAYOUT', r'page setup|slide size|page orientation|page size|columns dropdown|two columns'),
    ('PAGE_DECORATION', r'watermark|page colou?r'),
    ('TEXT_FORMAT', r'font|text colou?r|strikethrough|superscript|subscript|bold|italic|underline|字号|字体'),
    ('PARAGRAPH_FORMAT', r'paragraph|line spacing|center.align|bullet|drop cap'),
    ('DATA_SORT', r'\bsort(?:ing)?\b|ascending|descending|升序|降序|排序'),
    ('DATA_DEDUP', r'remove duplicates|删除重复'),
    ('IMAGE_APPEARANCE', r'sepia|picture colou?r|image colou?r'),
    ('IMAGE_INSERT', r'insert picture|insert image|picture button|image insertion|picture dropdown'),
    ('OBJECT_TRANSFORM', r'resize|rotate|aspect ratio'),
    ('TABLE_INSERT', r'insert table|\d\s*[x×]\s*\d\s*table'),
    ('TABLE_STRUCTURE', r'(?:insert|delete|add).{0,20}(?:row|column)'),
    ('TABLE_FORMAT', r'table style|table border|borders and shading'),
    ('FIND_REPLACE', r'find and replace|replace all'),
    ('TRANSLATE', r'translat|翻译'),
    ('COMMENT_EDIT', r'(?:new|add|delete|edit).{0,15}comment|新建批注|删除批注'),
    ('CHART_INSERT', r'chart type|chart dropdown|scatter plot|recommended charts|insert chart'),
    ('SMARTART', r'smartart'), ('TEXTBOX', r'text box|textbox'), ('WORDART', r'wordart'),
    ('SHAPE_INSERT', r'shapes dropdown|shape menu|line shape|rectangle shape'),
    ('EQUATION', r'insert equation|square root equation'),
    ('SPELLCHECK', r'spell(?:ing)? check'), ('READ_ALOUD', r'read aloud'),
    ('PRINT_PREVIEW', r'print preview'), ('DOC_PROTECT', r'encrypt|protect workbook|protect document'),
]


def local_jsonl(path):
    with path.open('rb') as f:
        while True:
            line = f.readline(16 * 1024 * 1024 + 1)
            if not line:
                return
            if len(line) > 16 * 1024 * 1024:
                raise ValueError('Local JSONL row exceeds bound')
            yield json.loads(line)


def fingerprint(task):
    # Task prompts can be rewritten for the same action trace. Deduplicate exact
    # action+code traces only, not merely identical resulting token sequences.
    payload = [(s.get('value', {}).get('action'), s.get('value', {}).get('code')) for s in task['traj']]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def label_step(step):
    v = step.get('value', {})
    action, code = v.get('action') or '', v.get('code') or ''
    if 'computer.terminate(' in code:
        return 'TERMINAL', 'marker', 'source_termination'
    if not code.strip():
        return 'OOV:UNKNOWN', 'unresolved', 'missing_executable_action'
    head = re.split(r'\bto\b|\bin order to\b|\bThis is\b', action, maxsplit=1)[0]
    lead = (v.get('observation') or '').split('\n\n')[0][:350]
    if re.search(r'(?:WPS|Word|Excel|PowerPoint).{0,20}icon.*(?:launch|open)|launch.{0,30}WPS', action, re.I):
        return 'OOV:APP_LAUNCH', 'action_recorded', 'application_launch'
    office = re.search(r'WPS|Microsoft (?:Word|Excel|PowerPoint)|(?:spreadsheet|word processing|presentation) (?:application|interface)', lead, re.I)
    external = re.search(r'Chrome|Edge browser|Safari|Firefox|WeChat|Zimbra|RStudio|Windows desktop|Windows Store|Microsoft Store|Google Sheets|Google Docs|Google Slides|Slack|web browser', lead, re.I)
    if external and (not office or external.start() < office.start()):
        return 'OOV:EXTERNAL', 'action_recorded', 'external_foreground_text'
    if not office and not re.search(r'\bWPS\b', head, re.I):
        return 'OOV:UNKNOWN', 'unresolved', 'office_context_not_established'
    if re.match(r'(?:type|paste|write|clear)\b', head, re.I):
        if re.search(r'filename|file name|save.{0,10}dialog', action, re.I):
            return 'SUPPORT', 'support', 'file_dialog_input'
        if re.search(r'formula|=\w+\(', action, re.I):
            return 'OOV:FORMULA_CALC', 'action_recorded', 'formula_input'
        if re.search(r'font.{0,15}(?:field|box|size)', action, re.I):
            return 'TEXT_FORMAT', 'action_recorded', 'font_parameter_input'
        if re.search(r'search|address|dialog|input field', action, re.I):
            return 'OOV:UNKNOWN', 'unresolved', 'input_target_ambiguous'
        if re.search(r'(?:into|in) (?:the )?(?:document|cell|slide|paragraph)', action, re.I):
            return 'TEXT_INSERT', 'action_recorded', 'body_text_input'
        return 'OOV:UNKNOWN', 'unresolved', 'input_target_not_established'
    # Selection and generic navigation do not establish the business operation
    # mentioned only as a future goal in the action description.
    if re.match(r'(?:select\b|click on cell\b|click on the .{0,30}(?:tab|menu)\b|right.click\b|scroll\b)', head, re.I):
        return 'SUPPORT', 'support', 'selection_or_navigation'
    if re.match(r'drag\b',head,re.I) and re.search(r'\bselect\b',action,re.I):
        return 'SUPPORT', 'support', 'selection_drag'
    for label, pattern in RULES:
        if re.search(pattern, head, re.I):
            entry = bool(re.search(r'\b(?:access|begin|open .*dialog|open .*menu)\b', action, re.I))
            applied = bool(re.search(r'\b(?:apply|execute|confirm|finalize|complete saving)\b', action, re.I))
            return label, 'entry_only' if entry and not applied else 'action_recorded', 'rule:' + label
    if re.search(r'open.{0,40}(?:file|document|xlsx|docx|pptx)', head, re.I):
        return 'OOV:DOC_OPEN', 'action_recorded', 'document_open'
    if re.search(r'press.*(?:enter|escape)|click.*(?:OK|Cancel|确定|取消)', head, re.I):
        return 'SUPPORT', 'support', 'generic_confirmation'
    return 'OOV:UNKNOWN', 'unresolved', 'no_safe_fine_mapping'


def automatic_spans(task):
    spans, pending = [], []
    for i, step in enumerate(task['traj']):
        label, support, rule = label_step(step)
        if label == 'SUPPORT':
            pending.append(i)
            continue
        if pending and spans and spans[-1]['label'] == label and label != 'TERMINAL':
            spans[-1]['positions'] += pending
        elif pending:
            # Supports may be prep for the following event, not an event itself.
            pass
        positions = pending + [i] if not spans or spans[-1]['label'] != label else [i]
        pending = []
        if spans and spans[-1]['label'] == label and label != 'TERMINAL':
            spans[-1]['positions'] += positions
            spans[-1]['rules'].append(rule)
            if support == 'action_recorded':
                spans[-1]['support'] = support
        else:
            spans.append(dict(label=label, support=support, positions=positions, rules=[rule]))
    if pending:
        spans.append(dict(label='OOV:SUPPORT_ONLY', support='unresolved', positions=pending, rules=['unassigned_support']))
    covered = [i for s in spans for i in s['positions']]
    if sorted(covered) != list(range(len(task['traj']))):
        raise ValueError('Step coverage not conserved')
    return spans


def manual_spans(task, spec):
    prefix = task['task_id'].split('_')[0]
    if fingerprint(task) != MANUAL_TRACE_HASHES[prefix]:
        raise ValueError('Manually reviewed action trace changed')
    spans = [dict(label=label, support=support, positions=list(range(a, b + 1)), rules=['manual_full_task_text_review'])
             for a, b, label, support in spec]
    if [i for s in spans for i in s['positions']] != list(range(len(task['traj']))):
        raise ValueError('Manual annotation must cover complete task without overlaps')
    for s in spans:
        if s['label'] == 'TERMINAL' and any('computer.terminate(' not in (task['traj'][i]['value'].get('code') or '') for i in s['positions']):
            raise ValueError('Manual terminal is not a termination step')
    return spans


def decorate(task, spans, mapping, mode, aliases):
    events = []
    for n, span in enumerate(spans):
        label = span['label']
        fine = label if label.startswith('OOV:') or label == 'TERMINAL' else 'WPSV1.' + label
        token = mapping[fine] if fine.startswith('WPSV1.') else fine
        events.append(dict(event_id=task['task_id'] + ':' + str(n), fine_label=fine,
            wss_token=token, support=span['support'], source_positions=span['positions'],
            boundary_method=mode, rules=sorted(set(span['rules'])), completion_verified=False,
            evidence=[dict(position=i, source_index=task['traj'][i].get('index'),
                source_step_id=task['traj'][i].get('step_id'),
                action=task['traj'][i].get('value', {}).get('action'),
                code=task['traj'][i].get('value', {}).get('code')) for i in span['positions']]))
    return dict(task_id=task['task_id'], aliases=aliases, trace_sha256=fingerprint(task),
        instruction=task.get('instruction'), task_completed_source_label=task.get('task_completed'),
        source_has_termination=any(e['fine_label']=='TERMINAL' for e in events),
        source_steps=len(task['traj']), annotation_mode=mode, events=events,
        not_a_human_usage_frequency_sample=True, screen_or_replay_verified=False)


def ngrams(sequences, level, include_oov=False, min_tasks=1):
    counts, tasks, denominators = Counter(), defaultdict(set), Counter()
    for seq in sequences:
        run = []
        runs = []
        for event in seq['events']:
            label = event[level]
            allowed = event['support'] == 'action_recorded' and label != 'TERMINAL'
            allowed = allowed and label != 'OOV:UNKNOWN' and (include_oov or not label.startswith('OOV:'))
            if allowed:
                run.append(label)
            else:
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        for run in runs:
            # Do not collapse equal parent tokens: two distinct fine events may
            # map to the same parent, producing a real parent self-transition.
            for n in range(2, 6):
                for i in range(len(run) - n + 1):
                    key = tuple(run[i:i+n])
                    counts[key] += 1
                    tasks[key].add(seq['task_id'])
                    denominators[(n, key[:-1])] += 1
    return [dict(n=len(k), sequence=list(k), occurrences=c,
                 distinct_tasks=len(tasks[k]), task_ids=sorted(tasks[k]),
                 prefix_observed_successors=denominators[(len(k), k[:-1])],
                 conditional_probability=c/denominators[(len(k), k[:-1])],
                 low_support=len(tasks[k]) < 5)
            for k, c in sorted(counts.items(), key=lambda x: (len(x[0]), -len(tasks[x[0]]), -x[1], x[0]))
            if len(tasks[k]) >= min_tasks]


def run(workspace, agentnet_root):
    g = DiskGuard(workspace)
    g.check()
    root = Path(agentnet_root)
    source_paths = [root/'wps_candidates/trajectories.jsonl', root/'wps_review/review.jsonl']
    source_hashes = {str(p): digest(p) for p in source_paths}
    review = {r['task_id']: r for r in local_jsonl(source_paths[1])}
    variants = defaultdict(list)
    for row in local_jsonl(source_paths[0]):
        if row['task_id'] not in review:
            raise ValueError('Trajectory missing local review')
        if review[row['task_id']]['decision'] == 'supported_desktop':
            variants[row['task_id']].append(row)
    chosen = []
    variant_audit = []
    for task_id, versions in sorted(variants.items()):
        # Prefer enriched variant; deterministic tie-break retains source order.
        selected = max(versions, key=lambda t: (bool(t.get('actual_task')), len(t['traj'])))
        chosen.append(selected)
        if len(versions)>1:
            variant_audit.append(dict(task_id=task_id, selected_trace_sha256=fingerprint(selected),
                policy='prefer enriched actual_task, then longest trajectory, then source order',
                variants=[dict(trace_sha256=fingerprint(t), steps=len(t['traj']), has_actual_task=bool(t.get('actual_task'))) for t in versions]))
    groups = defaultdict(list)
    for task in chosen:
        groups[fingerprint(task)].append(task)
    _, mapping = vocabulary(list(catalog().values()))
    provisional, audited, dedup = [], [], []
    for fingerprint_value, versions in sorted(groups.items()):
        reviewed = [(t, MANUAL[t['task_id'].split('_')[0]]) for t in versions if t['task_id'].split('_')[0] in MANUAL]
        task = reviewed[0][0] if reviewed else versions[0]
        aliases = [t['task_id'] for t in versions if t['task_id'] != task['task_id']]
        provisional.append(decorate(task, automatic_spans(task), mapping, 'heuristic_unreviewed', aliases))
        meta = dict(system=review[task['task_id']]['system'],
                    application_metadata=review[task['task_id']]['applications'],
                    component_candidates=review[task['task_id']]['components'],
                    task_id_suffix=task['task_id'].split('_',1)[-1])
        provisional[-1].update(meta)
        if reviewed:
            audited.append(decorate(task, manual_spans(task, reviewed[0][1]), mapping, 'manual_text_review', aliases))
            audited[-1].update(meta)
        dedup.append(dict(canonical_task_id=task['task_id'], aliases=aliases, trace_sha256=fingerprint_value))
    if len(audited) != len(MANUAL):
        raise ValueError('Manual task prefixes did not uniquely resolve')
    suffix_groups = defaultdict(list)
    for t in provisional:
        suffix_groups[t['task_id_suffix']].append(t['task_id'])
    summary = dict(local_candidate_tasks=len(review), prior_screening=dict(Counter(r['decision'] for r in review.values())),
        selected_task_ids=len(chosen), unique_action_traces=len(provisional), exact_trace_aliases=len(chosen)-len(provisional),
        duplicate_id_extra_variants=sum(len(v)-1 for v in variants.values()),
        selected_steps_after_dedup=sum(t['source_steps'] for t in provisional),
        manually_reviewed_traces=len(audited), manual_sampling='purposive examples covering sorting, editing, images, saves and external-app boundaries; not representative',
        tasks_without_termination=sum(not t['source_has_termination'] for t in provisional),
        source_completion_labels=dict(Counter(str(t['task_completed_source_label']) for t in provisional)),
        system_distribution=dict(Counter(t['system'] for t in provisional)),
        shared_task_id_suffix_groups=sum(len(ids)>1 for ids in suffix_groups.values()),
        provisional_event_labels=dict(Counter(e['fine_label'] for t in provisional for e in t['events'])),
        provisional_support=dict(Counter(e['support'] for t in provisional for e in t['events'])),
        rule_covered_fine_labels=sorted({label for label, _ in RULES if not label.startswith('OOV:')} | {'TEXT_INSERT'}),
        segmentation_limit='Consecutive same-label rule hits may merge distinct repeated operations; output is provisional until reviewed',
        no_network=True, no_training=True, no_replay=True, source_hashes=source_hashes,
        probability_definition='unsmoothed empirical conditional frequency, among adjacent resolved action-recorded events with an observed eligible successor; unknown/entry/OOV barriers retained',
        source_scope='Existing 697-task WPS-related local candidate extraction, not all AgentNet or all Office tasks')
    previous = g.path(OUT+'/manifest.json')
    if previous.exists():
        g.check()
        previous.unlink()
    artifacts = {}
    def put(name, data):
        with g.open('.tmp/agentnet-seq-'+name) as f:
            f.write(data)
        g.commit('.tmp/agentnet-seq-'+name, OUT+'/'+name)
        artifacts[name] = digest(g.path(OUT+'/'+name))
    def js(name, data):
        put(name, (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode())
    js('summary.json', summary)
    js('trace_deduplication.json', dedup)
    js('duplicate_id_variants.json',variant_audit)
    js('potential_related_tasks.json',[dict(task_id_suffix=k,task_ids=v,interpretation='shared UUID suffix; possible source correlation, not auto-merged')
                                      for k,v in suffix_groups.items() if len(v)>1])
    js('provisional_sequences.json', provisional)
    js('reviewed_sequences.json', audited)
    js('screening.json', [dict(task_id=k, decision=v['decision'], selected=k in variants) for k,v in review.items()])
    stats = {}
    for name, sequences in [('provisional',provisional), ('reviewed',audited)]:
        for level in ['fine_label', 'wss_token']:
            for oov in [False, True]:
                label = name+'_'+level+('_with_oov' if oov else '_strict')
                records = ngrams(sequences, level, include_oov=oov)
                js(label+'.json', records)
                if records:
                    put(label+'.csv', csv_bytes(records))
                stats[label] = records
        presence = defaultdict(set)
        for t in sequences:
            for e in t['events']:
                if e['support'] == 'action_recorded' and e['fine_label'] != 'TERMINAL':
                    presence[e['fine_label']].add(t['task_id'])
        js(name+'_task_operation_presence.json', [dict(fine_label=k, tasks=len(v), denominator_tasks=len(sequences), task_fraction=len(v)/len(sequences), task_ids=sorted(v))
                                                 for k,v in sorted(presence.items(),key=lambda x:-len(x[1]))])
    js('provisional_transitions_by_system.json', {system:dict(tasks=sum(t['system']==system for t in provisional),
        strict_wss_token_ngrams=ngrams([t for t in provisional if t['system']==system],'wss_token'))
        for system in sorted({t['system'] for t in provisional})})
    # Compare automatic segmentation to manual complete-task review. This is a
    # targeted audit, not a random-sample accuracy estimate.
    auto = {t['task_id']:t for t in provisional}
    checks = []
    for task in audited:
        a = [e['fine_label'] for e in auto[task['task_id']]['events'] if e['fine_label'] != 'TERMINAL']
        b = [e['fine_label'] for e in task['events'] if e['fine_label'] != 'TERMINAL']
        checks.append(dict(task_id=task['task_id'], automatic=a, reviewed=b, exact_sequence_match=a==b))
    js('manual_audit_comparison.json', checks)
    audit_summary=dict(reviewed_tasks=len(checks),exact_sequence_matches=sum(c['exact_sequence_match'] for c in checks),
        interpretation='purposive audit, not population accuracy; automatic sequences not approved for behavior modeling')
    js('manual_audit_summary.json',audit_summary)
    lines = ['# AgentNet 语义操作序列分析 v1', '',
        '只读本地 AgentNet 文本，完整保留任务边界；未下载、未执行原始 code、未回放 WPS 或训练预测模型。', '',
        '## 范围与可信度', '',
        f"本地候选 {len(review)} 个任务；沿用前次文本初筛选中 {len(chosen)} 个桌面 WPS 相关 ID，"
        f"按完全相同 action+code 去重后 {len(provisional)} 条轨迹、{summary['selected_steps_after_dedup']} 步。"
        f"另对 {len(audited)} 条有目的选取的完整任务做文本人工分段审阅。", '',
        '305 个初筛任务不是可靠的纯 WPS 集合，含浏览器、公式教程及背景应用提及。'
        '自动结果是待审阅候选；人工结果也是 action/code 文本证据，不是截图或执行验证。'
        '人工子集用于验证边界与展示序列，不用于推断一般用户频率。没有将不同任务拼接成用户会话。', '',
        f"自动结果在这 {len(checks)} 条任务上仅 {audit_summary['exact_sequence_matches']} 条细标签序列与人工整段结果完全一致。"
        f"全部自动候选包含 {summary['provisional_support'].get('unresolved',0)} 个未解决语义段。"
        '这不是随机抽样准确率，但明确表明自动分段仍不能直接用于行为建模。', '',
        f"有 {summary['tasks_without_termination']} 条所选轨迹没有明确终止动作；不将它们视为已验证完整执行。"
        f"另有 {summary['shared_task_id_suffix_groups']} 组任务共享 ID 的 UUID 后缀，标记为潜在相关来源。"
        '因 action/code 不完全一致而未自动合并，任务支持数不等于独立用户数。重复 ID 版本选择单独审计。', '',
        '当前映射优先保证保守性，未覆盖的细标签返回 OOV:UNKNOWN；不根据任务请求补造未发生的动作。'
        '表格公式计算不等于 WT29 数学公式对象；新建工作表不等于插入 Word 表格；'
        '图片裁剪/替换/阴影超出当前细标签明确边界，保留 OOV，不悄悄扩展 65/30 定义。', '',
        '## 序列如何构成', '',
        '选区、功能区导航和确认步骤合入语义候选段，同一连续候选标签可归为一段。'
        '自动合段可能合并重复操作或受意图描述干扰，保留每一步原 action/code、分段来源和质量等级。'
        '人工审阅按完整任务分段；终止标记不算操作。一次任务可能从已打开文档开始，不补造 DOC_OPEN。', '',
        '词表外节点使用 OOV:DOC_OPEN、OOV:DOC_NEW、OOV:FORMULA_CALC、OOV:EXTERNAL 等侧标签，'
        '不占用现有 30 token。未知节点、仅入口及缺证节点都是严格统计的断点，不能删除后跨越计算邻接。'
        'with_oov 表保留已识别词表外节点；OOV:UNKNOWN 仍为断点。相邻不同细操作映射到同一 token 时保留自转移。', '',
        '## 人工审阅的完整任务序列', '']
    for t in audited:
        seq = [e['fine_label'].removeprefix('WPSV1.') + ('[仅入口]' if e['support']=='entry_only' else '') for e in t['events'] if e['fine_label']!='TERMINAL']
        lines += [f"- `{t['task_id']}`："+' → '.join(seq)]
    lines += ['', '## 概率与子序列口径', '',
        '`P(B|A)=count(A,B)/sum_X count(A,X)`；`P(C|A,B)=count(A,B,C)/sum_X count(A,B,X)`。'
        '分母仅包含同任务连续、证据可纳入且有下一步的观察窗口。末尾操作不虚构后继；'
        '严格版本排除的窗口不算入分母。因此是“可观测合格相邻窗口内”的条件频率，不是总体概率。', '',
        '同时输出出现次数、不同去重任务支持数、支持任务 ID 和分母；少于 5 个任务统一标记 low_support。'
        'n=2 对应一阶，n=3 对应二阶；n=4/5 为更长连续子序列。所有窗口保留重复计数，'
        '不同任务支持数用来防止一条长任务主导“常见”判断；没有训练 Markov 模型或生成序列。', '',
        '只有至少两个不同去重任务支持的子序列才作为“重复出现”列出，不意味着固定习惯或统计显著。'
        '相似教程/改写任务仍可能相关，exact trace 去重无法消除全部来源偏差。', '',
        '## 人工子集：重复出现的 token 邻接', '',
        '|相邻 token|次数|任务支持|条件频率及分母|', '|---|---:|---:|---:|']
    for r in stats['reviewed_wss_token_strict']:
        if r['n']==2 and r['distinct_tasks']>=2:
            lines.append(f"|{' → '.join(r['sequence'])}|{r['occurrences']}|{r['distinct_tasks']}|{r['conditional_probability']:.3f} ({r['occurrences']}/{r['prefix_observed_successors']})|")
    lines += ['', '## 自动候选：需审阅的高支持 token 邻接', '',
        '**以下不能作为已验证操作规律。** 自动标注的准确率尚未建立，分段错误会直接影响概率。', '',
        '|候选邻接|次数|任务支持|条件频率及分母|', '|---|---:|---:|---:|']
    for r in [r for r in stats['provisional_wss_token_strict'] if r['n']==2][:15]:
        lines.append(f"|{' → '.join(r['sequence'])}|{r['occurrences']}|{r['distinct_tasks']}|{r['conditional_probability']:.3f} ({r['occurrences']}/{r['prefix_observed_successors']})|")
    lines += ['', '## 人工子集：二阶条件频率', '',
        '以下仅有目的选择的少量任务支持，即便条件频率为 1 也不能据此判断为稳定规律。', '',
        '|连续三个 token|次数|任务支持|P(第三个∣前两个)|', '|---|---:|---:|---:|']
    triples = [r for r in stats['reviewed_wss_token_strict'] if r['n']==3]
    for r in triples:
        lines.append(f"|{' → '.join(r['sequence'])}|{r['occurrences']}|{r['distinct_tasks']}|{r['conditional_probability']:.3f} ({r['occurrences']}/{r['prefix_observed_successors']})|")
    lines += ['', '## 更长子序列的支持情况', '']
    for scope in ['reviewed','provisional']:
        for n in [3,4,5]:
            supported = [r for r in stats[scope+'_wss_token_strict'] if r['n']==n and r['distinct_tasks']>=2]
            lines.append(f'- {scope}：长度 {n}、至少 2 个去重任务支持的严格连续 token 子序列共 {len(supported)} 种。')
    lines += ['', '## 下一步使用与交付', '',
        '可以用 reviewed_sequences 查看有文本支持的任务链，并用 provisional_sequences 定位待审阅任务。'
        '需要先对按应用/任务类型抽样的轨迹做独立分段核验，完善公式计算、文档打开等 OOV 决策，'
        '再将概率用于用户会话生成。当前没有足够证据把候选条件频率称为真实用户习惯。', '',
        '- `reviewed_sequences.json` / `provisional_sequences.json`：完整任务分段，65 细标签及 30 token 映射、OOV 和原始步骤证据。',
        '- `*_task_operation_presence.json`：一个任务中有哪些操作及其任务支持数。',
        '- `*_fine_label_*.json/csv` / `*_wss_token_*.json/csv`：一阶、二阶和长度 4/5 连续子序列，含分母和来源。',
        '- `manual_audit_comparison.json`：自动分段与人工子集逐任务对比，不作为总体准确率。',
        '- `trace_deduplication.json` / `screening.json` / `summary.json`：筛选范围、重复版本及去重来源审计。', '',
        '复现：`PYTHONPATH=.deps PYTHONDONTWRITEBYTECODE=1 python3 -m wps_operation_dataset.agentnet_sequences`。'
        '输入路径可设 --agentnet-root；输入版本与输出哈希记录在 manifest。'
        '中断残留 .tmp/agentnet-seq-* 需检查移走后重跑。', '']
    put('REPORT.md', '\n'.join(lines).encode())
    if any(digest(Path(p))!=h for p,h in source_hashes.items()):
        raise ValueError('Local source changed during analysis')
    result = dict(completed=True, source_hashes=source_hashes, artifacts=artifacts.copy(),
        implementation_hashes={name:digest(Path(__file__).parent/name) for name in ['agentnet_sequences.py','semantic_v1.py','wss_tokens_v1.py']},
        reviewed_tasks=len(audited), provisional_tasks=len(provisional), storage_before_manifest=g.check())
    js('manifest.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--agentnet-root', default='/home/lzx/Desktop/AgentNet')
    args = parser.parse_args()
    print(json.dumps(run(args.workspace,args.agentnet_root),ensure_ascii=False,indent=2))
