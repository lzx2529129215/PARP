import json
from pathlib import Path
import socket

import pyarrow.parquet as pq
import pytest

from wps_operation_dataset.analyze_offline import digest
from wps_operation_dataset.semantic_v1 import catalog
from wps_operation_dataset.storage import DiskGuard
from wps_operation_dataset.wss_tokens_v1 import OUT, SOURCE, project_evidence, run, vocabulary

ROOT = Path(__file__).resolve().parents[1]


def test_total_unique_mapping_and_mechanism_boundaries():
    tokens, m = vocabulary(list(catalog().values()))
    assert len(tokens) == 30 and len(m) == 65
    assert sum(len(t['fine_operation_ids']) for t in tokens) == 65
    for a, b in [('TEXT_INSERT', 'TEXT_DELETE'), ('DOC_SAVE', 'DOC_CONVERT'), ('IMAGE_INSERT', 'IMAGE_APPEARANCE')]:
        assert m['WPSV1.'+a] == m['WPSV1.'+b]
    for a, b in [('TEXT_INSERT', 'TEXT_FORMAT'), ('PRINT_PREVIEW', 'VIEW_LAYOUT'),
                 ('IMAGE_INSERT', 'MODEL_3D'), ('TABLE_STRUCTURE', 'TABLE_FORMAT'),
                 ('PAGE_LAYOUT', 'PAGE_DECORATION'), ('EQUATION', 'SMARTART')]:
        assert m['WPSV1.'+a] != m['WPSV1.'+b]
    assert all(not t['empirical_WSS_validation'] for t in tokens)


def test_unknown_or_missing_fine_labels_rejected():
    with pytest.raises(ValueError, match='exactly'):
        vocabulary(list(catalog().values())[:-1])
    with pytest.raises(KeyError):
        project_evidence([{'operation_id': 'unknown'}], {})


def test_projection_preserves_evidence_and_does_not_create_events():
    _, m = vocabulary(list(catalog().values()))
    evidence = json.loads((ROOT / SOURCE / 'operation_evidence.json').read_text())
    output = project_evidence(evidence, m)
    assert len(output) == len(evidence)
    for before, after in zip(evidence, output):
        assert all(after[k] == v for k, v in before.items())
        assert not after['wss_training_ready'] and not after['event_boundary_verified']
    imported = [e for e in output if e['execution_id'] == 'excel_1_80']
    assert {(e['wss_token_id'], e['support']) for e in imported} == {
        ('WT18', 'evidence_gap_or_conflict'), ('WT11', 'action_recorded')}


def test_full_offline_projection_and_reproducibility(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('Unexpected network access')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    g = DiskGuard(tmp_path)
    files = list((ROOT / SOURCE).iterdir()) + [ROOT / 'data/curated/gui360_office.parquet']
    hashes = {}
    for p in files:
        relative = str(p.relative_to(ROOT))
        hashes[relative] = digest(p)
        with g.open(relative) as f:
            f.write(p.read_bytes())
    result = run(tmp_path)
    assert (result['fine_labels'], result['tokens'], result['trajectories'], result['steps']) == (65, 30, 121, 753)
    space = json.loads((tmp_path / OUT / 'operation_space.json').read_text())
    original = json.loads((ROOT / SOURCE / 'wps_operation_space_v1_candidates.json').read_text())
    assert space['fine_layer'] == original['candidates']
    steps = pq.read_table(tmp_path / OUT / 'step_annotations.parquet').to_pylist()
    for s in steps:
        if not s['is_functional_action']:
            assert s['wss_token_ids'] == []
    assert all(digest(tmp_path / p) == h for p, h in hashes.items())
    assert run(tmp_path)['artifacts'] == result['artifacts']
    p = tmp_path / SOURCE / 'operation_evidence.json'
    p.write_text('[]')
    with pytest.raises(ValueError, match='artifact changed'):
        run(tmp_path)
