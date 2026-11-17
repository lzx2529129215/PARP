"""Whole-plan qualification by total faults, never by a single operation's faults."""
import json
from pathlib import Path
from execution_contract import SCHEMA,digest,sha
from total_faults import compare_totals


def assess(controls,treated,contract,scope):
    if any(r.get('stage')!='baseline'for r in controls)or any(r.get('stage')!='calibration'for r in treated):
        raise ValueError('fresh same-service controls/calibration required')
    if any(r['execution_contract']!=contract for r in controls+treated):raise ValueError('conditions differ')
    score=compare_totals(controls,treated,scope)
    # A zero-action policy must not be described as a qualified reclaim amount.
    def intervention(row):
        requested=any((w['name']=='memory.reclaim' and int(w['value'])>0) or
            (w['name']=='memory.high' and str(w['value'])!='max' and
             w.get('before_bytes',0)>int(w['value']))
            for writes in row.get('writes',{}).values()for w in writes)
        # Merely installing a fixed cap above current usage is not reclamation.
        # Later automatic high pressure is visible in the existing domain events.
        high_events=row.get('final',{}).get('events',{}).get('high',0)-row.get('initial',{}).get('events',{}).get('high',0)
        return requested or high_events>0
    exercised=all(intervention(r)for r in treated)
    return dict(eligible=score['within_trial_limit']is True and exercised,
        reason='total_fault_limit_passed'if score['within_trial_limit']is True and exercised else
               'no_actual_intervention'if not exercised else 'total_fault_limit_not_passed',
        total_faults=score,latency_qualified=False,space_target_qualified=False,
        scope='entire same-service multi-app proposal, not independent donor certificates')


def publish(path,controls_paths,treated_paths,contract,scope,*,root):
    root=Path(root).resolve();path=Path(path)
    if path.exists():raise ValueError('preserve existing qualification')
    def read(paths):
        rows=[];items=[]
        for p in paths:
            p=Path(p).resolve()
            if not p.is_relative_to(root):raise ValueError('new evidence must be inside this experiment')
            rows.append(json.loads(p.read_text(encoding='utf-8')))
            items.append(dict(path=p.relative_to(root).as_posix(),sha256=sha(p)))
        return rows,items
    off,off_items=read(controls_paths);on,on_items=read(treated_paths)
    result=assess(off,on,contract,scope)
    doc=dict(schema=SCHEMA,execution_contract=contract,execution_contract_sha256=digest(contract),
        controls=off_items,trials=on_items,assessment=result)
    path.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return doc


def load_qualified(path,contract,scope,*,root):
    root=Path(root).resolve();path=Path(path)
    if not path.exists():raise ValueError('no matching total-fault calibration; old tables cannot grant access')
    doc=json.loads(path.read_text(encoding='utf-8'))
    if (doc.get('schema')!=SCHEMA or doc.get('execution_contract')!=contract or
            doc.get('execution_contract_sha256')!=digest(contract)):
        raise ValueError('scope or calibration/formal conditions changed')
    def read(items):
        rows=[]
        for item in items:
            p=(root/item['path']).resolve()
            if not p.is_relative_to(root)or sha(p)!=item['sha256']:raise ValueError('raw qualification evidence changed')
            rows.append(json.loads(p.read_text(encoding='utf-8')))
        return rows
    result=assess(read(doc['controls']),read(doc['trials']),contract,scope)
    if not result['eligible']:raise ValueError('whole-plan total-fault calibration did not pass')
    return result,sha(path)
