import copy
import json
from pathlib import Path
import socket

import pyarrow.parquet as pq
import pytest

from wps_operation_dataset.analyze_offline import INPUT, OUT, digest, run, summarize
from wps_operation_dataset.semantic_v1 import SOURCE_SHA256, annotations
from wps_operation_dataset.storage import DiskGuard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rows():
    return pq.read_table(ROOT / INPUT).to_pylist()


def test_real_source_and_distribution_conservation(rows):
    assert digest(ROOT / INPUT) == SOURCE_SHA256
    s, ts, steps, ev, cs = summarize(rows)
    assert (s['trajectories'], s['raw_steps'], s['functional_steps'], s['non_action_records']) == (121, 753, 621, 132)
    assert sum(s['family_primary_distribution'].values()) == 121
    assert sum(c['primary_intent_count'] for c in cs.values()) == 121
    assert sum(s['step_role_distribution'].values()) == 753
    assert s['functional_action_types'] == {'GUI': 606, 'API': 15}
    assert {a['app_domain']: a['trajectories'] for a in s['application_distribution']} == dict(word=63, excel=21, ppt=37)
    assert len(ts) == 121 and len(steps) == 753
    assert len(cs) == 65 and all(c['source_trajectory_ids'] for c in cs.values())
    assert all(not e['completion_verified'] for e in ev)


def test_terminal_status_is_not_a_reason_to_drop_real_actions(rows):
    s, _, steps, _, _ = summarize(rows)
    assert s['functional_steps_with_terminal_status'] == 48
    row = next(x for x in steps if x['execution_id'] == 'word_1_183' and x['step_id'] == 3)
    assert row['is_functional_action'] and row['role'] == 'semantic_anchor'
    assert s['no_action_trajectories'] == ['ppt_1_230', 'word_2_125', 'word_2_210']


def test_conflicts_and_weak_evidence_are_preserved(rows):
    _, _, _, ev, cs = summarize(rows)
    excel = [e for e in ev if e['execution_id'] == 'excel_1_80']
    assert {(e['operation_id'], e['support']) for e in excel} == {
        ('WPSV1.DATA_IMPORT', 'evidence_gap_or_conflict'), ('WPSV1.DOC_SAVE_AS', 'action_recorded')}
    assert cs['DATA_IMPORT']['recorded_action_trajectory_count'] == 0
    assert cs['SLIDE_BACKGROUND']['support_trajectory_counts'] == {'intent_only': 1}
    assert next(e for e in ev if e['execution_id'] == 'ppt_1_214')['semantic_evidence'] == 'coordinate_and_request'
    assert next(e for e in ev if e['execution_id'] == 'excel_1_112')['support'] == 'action_recorded'


def test_repeated_actions_do_not_become_repeated_semantic_operations(rows):
    s, _, _, ev, cs = summarize(rows)
    assert s['repeated_action_signatures'] == 96
    assert len([e for e in ev if e['execution_id'] == 'word_1_56']) == 1
    assert cs['TABLE_INSERT']['recorded_action_trajectory_count'] == 4
    assert all(len(c['source_trajectory_ids']) == len(set(c['source_trajectory_ids'])) for c in cs.values())


@pytest.mark.parametrize('kind', ['duplicate_step', 'missing_trajectory', 'unknown_candidate', 'empty_anchor', 'missing_anchor', 'non_action_anchor', 'unknown_secondary', 'request_drift'])
def test_bad_input_or_annotations_fail_closed(rows, kind):
    a = annotations()
    kwargs = {'mapping': a}
    if kind == 'duplicate_step':
        rows.append(copy.deepcopy(rows[0]))
    elif kind == 'missing_trajectory':
        rows = [r for r in rows if r['execution_id'] != 'word_1_103']
    elif kind == 'unknown_candidate':
        a['word_1_103']['candidate'] = 'UNKNOWN'
    elif kind == 'empty_anchor':
        a['word_1_103']['anchor_steps'] = []
    elif kind == 'missing_anchor':
        a['word_1_103']['anchor_steps'] = [999]
    elif kind == 'non_action_anchor':
        a['word_1_103']['anchor_steps'] = [3]
    elif kind == 'unknown_secondary':
        kwargs['secondary'] = {'unknown': [('TEXT_INSERT', [1])]}
    elif kind == 'request_drift':
        rows[0]['request'] = 'changed'
    with pytest.raises(ValueError):
        summarize(rows, **kwargs)


def test_offline_run_is_reproducible_and_source_preserved(tmp_path, monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail('Offline analysis attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', reject_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', reject_network)
    monkeypatch.setattr(socket, 'create_connection', reject_network)
    monkeypatch.setattr(socket, 'getaddrinfo', reject_network)
    g = DiskGuard(tmp_path)
    with g.open(INPUT) as f:
        f.write((ROOT / INPUT).read_bytes())
    run(tmp_path)
    manifest = json.loads((tmp_path / OUT / 'manifest.json').read_text())
    assert manifest['completed'] and manifest['checks']['source_unchanged']
    assert digest(tmp_path / INPUT) == SOURCE_SHA256
    for name, info in manifest['artifacts'].items():
        assert digest(tmp_path / OUT / name) == info['sha256']
    first = manifest['artifacts']
    run(tmp_path)
    assert json.loads((tmp_path / OUT / 'manifest.json').read_text())['artifacts'] == first
    assert not list((tmp_path / '.tmp').iterdir())
    assert len(pq.read_table(tmp_path / OUT / 'step_annotations.parquet')) == 753


def test_changed_source_is_rejected_before_outputs(tmp_path):
    with DiskGuard(tmp_path).open(INPUT) as f:
        f.write(b'changed')
    with pytest.raises(ValueError, match='SHA-256'):
        run(tmp_path)
    assert not (tmp_path / OUT).exists()


def test_interrupted_publication_invalidates_completion_then_can_resume(tmp_path, monkeypatch):
    g = DiskGuard(tmp_path)
    with g.open(INPUT) as f:
        f.write((ROOT / INPUT).read_bytes())
    run(tmp_path)
    original = DiskGuard.commit
    def fail_commit(self, source, destination):
        if destination.endswith('summary.json'):
            raise OSError('simulated interrupted publication')
        return original(self, source, destination)
    monkeypatch.setattr(DiskGuard, 'commit', fail_commit)
    with pytest.raises(OSError):
        run(tmp_path)
    assert not (tmp_path / OUT / 'manifest.json').exists()
    # Partial data are retained for inspection, not silently overwritten on restart.
    assert (tmp_path / '.tmp/offline-summary.json').exists()
    monkeypatch.setattr(DiskGuard, 'commit', original)
    with pytest.raises(FileExistsError):
        run(tmp_path)
    (tmp_path / '.tmp/offline-summary.json').unlink()
    run(tmp_path)
    assert json.loads((tmp_path / OUT / 'manifest.json').read_text())['completed']
