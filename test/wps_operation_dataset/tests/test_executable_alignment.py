import json
from pathlib import Path
import socket
import subprocess

import pytest

from wps_operation_dataset.executable_alignment import (AUTOMATION, OUT, TASK_REVIEW,
    is_ready, registry_inventory, run, sha, symbols, task_alignment)


def test_registry_placeholders_are_not_executable():
    rows={r['operation_id']:r for r in registry_inventory(AUTOMATION/'semantic/operations/wps_operations.json')}
    assert rows['WPS_INSERT_IMAGE']['status']=='placeholder'
    assert rows['WPS_PASTE_IMAGE']['status']=='placeholder'
    assert rows['WPS_SAVE_AS']['status']=='declared_actions_only'
    assert not any(r['runtime_verified'] for r in rows.values())


def test_static_parser_does_not_execute_top_level_code(tmp_path):
    p=tmp_path/'fake.py';p.write_text('raise RuntimeError("must not execute")\nclass UI:\n def open(self,path): pass\n')
    assert symbols(p)['UI.open']['parameters']==['self','path']


def test_composite_and_external_gaps_block_complete_replay():
    task=dict(task_id='x',instruction='save and send',traj=[{'value':{'action':'save','code':'...'}},{'value':{'action':'send','code':'...'}}])
    result=task_alignment(task,[(0,0,['EX09'],'parameter_bindable','save'),(1,1,[],'external_dependency','email')])
    assert not result['all_semantic_actions_bindable']
    assert not is_ready(result)
    with pytest.raises(ValueError,match='all source steps'):
        task_alignment(task,[(0,0,['EX09'],'parameter_bindable','save')])


def test_binding_without_fixture_or_postcondition_is_not_ready():
    r=dict(all_semantic_actions_bindable=True,fixture_bound=False,blockers=[])
    assert not is_ready(r)
    r['fixture_bound']=True
    assert not is_ready(r)
    r['task_postcondition_implemented']=True
    assert is_ready(r)
    r['blockers']=['calibration_missing']
    assert not is_ready(r)


def test_full_alignment_never_launches_gui_or_network(tmp_path,monkeypatch):
    def deny(*args,**kwargs):pytest.fail('GUI/subprocess/network action attempted during static alignment')
    monkeypatch.setattr(subprocess,'Popen',deny)
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)
    monkeypatch.setattr(socket,'create_connection',deny)
    monkeypatch.setattr(socket,'getaddrinfo',deny)
    summary=run(tmp_path)
    assert summary['individually_aligned_tasks']==len(TASK_REVIEW)==14
    assert summary['conditional_full_action_plans']==1
    assert summary['ready_to_replay_tasks']==0
    assert summary['capabilities']==22
    output=tmp_path/OUT
    space=json.loads((output/'operation_space.json').read_text())
    new_word=next(c for c in space['capabilities'] if c['capability_id']=='EX03')
    assert new_word['kind']=='composite' and new_word['historical_ui_audit_passes']==5
    # PARTIAL memory coverage does not erase a historical GUI audit pass.
    assert all(e['memory_status']=='PARTIAL' for e in new_word['historical_evidence'])
    assert not new_word['current_version_replay_verified']
    manifest=json.loads((output/'manifest.json').read_text())
    assert all(sha(output/name)==h for name,h in manifest['artifacts'].items())
    plan=json.loads((output/'first_batch_conditional_plan.json').read_text())
    assert not plan['ready_to_replay'] and not plan['executed']
    assert json.loads((output/'ready_to_replay.json').read_text())==[]
    assert not any('WPSV1.' in str(x) or 'wss_token' in str(x) for x in space['capabilities'])
