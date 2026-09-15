import copy
from pathlib import Path
import socket

import pytest

from wps_operation_dataset.agentnet_sequences import (automatic_spans, fingerprint, label_step,
    local_jsonl, manual_spans, MANUAL, ngrams, run)


def step(action, code='pyautogui.click(1,2)', observation='The WPS Office document interface is open.'):
    return {'value':dict(action=action,code=code,observation=observation)}


def seq(task_id, labels, support=None):
    return dict(task_id=task_id,events=[dict(fine_label=k,wss_token=k,
        support=(support or {}).get(i,'action_recorded')) for i,k in enumerate(labels)])


def test_first_second_order_denominators_and_task_support():
    sequences=[seq('one',['A','B','C','A','B','C']),seq('two',['A','B','D'])]
    rows={tuple(r['sequence']):r for r in ngrams(sequences,'fine_label')}
    assert rows['A','B']['occurrences']==3 and rows['A','B']['distinct_tasks']==2
    assert rows['A','B','C']['conditional_probability']==2/3
    assert rows['A','B','D']['conditional_probability']==1/3
    assert rows['A','B','C']['prefix_observed_successors']==3
    assert rows['A','B','C','A']['occurrences']==1


def test_never_join_tasks_or_bridge_unknown_or_entry_only():
    assert ngrams([seq('a',['A']),seq('b',['B'])],'fine_label')==[]
    for label in ['OOV:UNKNOWN','OOV:DOC_OPEN','TERMINAL']:
        assert ngrams([seq('a',['A',label,'B'])],'fine_label')==[]
    assert ngrams([seq('a',['A','X','B'],{1:'entry_only'})],'fine_label')==[]
    rows=ngrams([seq('a',['A','OOV:DOC_OPEN','B'])],'fine_label',True)
    assert ['A','B'] not in [r['sequence'] for r in rows]
    assert ['A','OOV:DOC_OPEN','B'] in [r['sequence'] for r in rows]
    assert ngrams([seq('a',['A','OOV:UNKNOWN','B'])],'fine_label',True)==[]


def test_parent_self_transition_and_repeated_window_preserved():
    rows=ngrams([seq('a',['WT11','WT11','WT11'])],'wss_token')
    r=next(r for r in rows if r['n']==2)
    assert r['occurrences']==2 and r['distinct_tasks']==1


def test_intended_sort_after_selection_is_not_an_executed_sort():
    assert label_step(step('Click on cell A1 to sort the table.'))[0]=='SUPPORT'
    assert label_step(step('Click on the Sort button to execute the sorting operation.'))[:2]==('DATA_SORT','action_recorded')
    assert label_step(step('Type x into the search field of the document.'))[0]=='OOV:UNKNOWN'
    assert label_step(step('Type "sort and save" into the document.'))[0]=='TEXT_INSERT'
    assert label_step(step('Type "sort.docx" as the filename in the Save dialog.'))[0]=='SUPPORT'


def test_external_and_out_of_vocabulary_are_not_forced_into_office_tokens():
    assert label_step(step('Click on Save.',observation='The Google Chrome browser is open, with WPS in the background.'))[0]=='OOV:EXTERNAL'
    assert label_step(step('Click on Insert Function to compute SUMIF.'))[0]=='OOV:FORMULA_CALC'
    assert label_step(step('Click on Crop to edit the image.'))[0]=='OOV:IMAGE_CROP'


def test_support_steps_and_terminal_conservation():
    task=dict(traj=[step('Select cells.'),step('Click on Sort.'),step('Click OK.'),step('Click on Sort.'),
                    step('Unknown operation.'),step('Click on Sort.'),step('Done.','computer.terminate(status="success")')])
    spans=automatic_spans(task)
    assert [s['label'] for s in spans]==['DATA_SORT','OOV:UNKNOWN','DATA_SORT','TERMINAL']
    assert [i for s in spans for i in s['positions']]==list(range(7))


def test_dedup_uses_action_trace_not_instruction_or_semantic_sequence():
    a=dict(instruction='first',traj=[step('Click Save.')]);b=copy.deepcopy(a);b['instruction']='rewritten'
    assert fingerprint(a)==fingerprint(b)
    b['traj'][0]['value']['code']='pyautogui.click(5,8)'
    assert fingerprint(a)!=fingerprint(b)


def test_manual_annotation_rejects_changed_trace():
    task={'task_id':'20240927235321_test','traj':[step('changed')]}
    with pytest.raises(ValueError,match='trace changed'):
        manual_spans(task,MANUAL['20240927235321'])


def test_full_local_analysis_offline(tmp_path,monkeypatch):
    def deny(*args,**kwargs):
        pytest.fail('Network access attempted')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)
    monkeypatch.setattr(socket,'getaddrinfo',deny)
    monkeypatch.setattr(socket,'create_connection',deny)
    root=Path('/home/lzx/Desktop/AgentNet')
    if not (root/'wps_candidates/trajectories.jsonl').exists():
        pytest.skip('Local AgentNet data not installed; no download attempted')
    result=run(tmp_path,root)
    assert result['reviewed_tasks']==9
    assert result['provisional_tasks']<=305
    assert result['completed']
    assert run(tmp_path,root)['artifacts']==result['artifacts']
