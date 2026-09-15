import contextlib
import io
import json
from collections import namedtuple
import pytest
import pyarrow.parquet as pq
from wps_operation_dataset.storage import DiskGuard, StorageLimitError, GiB, MiB
from wps_operation_dataset.remote import HFRemote, BoundedReader, allowed_path, APPS, PREFIXES
from wps_operation_dataset.extract import FIELDS, project_stream, extract
from wps_operation_dataset.cli import digest, validate_plan, report, save_report, make_plan

REV = 'a' * 40
Usage = namedtuple('Usage', 'total used free')


def step(app='word', execution='t1', index=0):
    return {'execution_id': execution, 'app_domain': app, 'request': 'Edit a report', 'step_id': index,
            'template': 'do_not_keep.docx', 'evaluation': {'reason': 'discard'},
            'step': {'screenshot_clean': 'https://invalid/image.png', 'screenshot_annotated': 'x.jpg',
                     'ui_tree': {'children': [{'name': 'discard accessibility data'}]},
                     'control_infos': {'uia_controls_info': [1, 2]}, 'thought': 'discard',
                     'status': 'CONTINUE', 'action': {'action_type': 'GUI', 'control_text': 'Save',
                       'control_label': '10', 'function': 'click',
                       'args': {'button': 'left', 'screenshot': 'data:image/png;base64,AAAA',
                                'nested': {'a11y': {'x': 1}, 'text': 'ok'}},
                       'coordinate_x': 99}}}


def body(*items):
    return ('\n'.join(json.dumps(x) for x in items) + '\n').encode()


@pytest.fixture
def guard(tmp_path):
    return DiskGuard(tmp_path / 'workspace')


@pytest.mark.parametrize('path', [
    'train/image/word/in_app/success/a.png', 'fail/data/word/in_app/success/a.jsonl',
    'processed_data/a.jsonl', 'train/data/word/search/success/a.jsonl',
    'train/data/word/online/success/a.jsonl', 'train/data/word/in_app/fail/a.jsonl',
    'test/data/word/in_app/success/a.jsonl', 'train/data/word/in_app/success/x/a.jsonl',
    'train/data/word/in_app/success/a.jpg', 'train/data/word/in_app/success/%2e%2e.jsonl',
    'train/data/word/in_app/success/a.jsonl?download=image', '../train/data/word/in_app/success/a.jsonl'])
def test_exact_allowlist_rejects(path):
    with pytest.raises(ValueError):
        allowed_path(path)


@pytest.mark.parametrize('app', APPS)
def test_allowlist_accepts(app):
    assert allowed_path(PREFIXES[app] + '/a.jsonl') == app


def test_disk_free_checked_before_every_write(guard):
    free = [21 * GiB]
    guard.disk_usage = lambda _: Usage(100 * GiB, 0, free[0])
    with guard.open('data/x') as f:
        f.write(b'good')
        free[0] = 20 * GiB
        with pytest.raises(StorageLimitError):
            f.write(b'bad')
    assert guard.path('data/x').read_bytes() == b'good'


def test_workspace_projected_quota(guard):
    guard.max_workspace = 5
    with guard.open('data/x') as f:
        f.write(b'12345')
        with pytest.raises(StorageLimitError):
            f.write(b'6')


def test_temp_quota(guard):
    guard.max_temp = 3
    with guard.open('.tmp/x') as f:
        f.write(b'123')
        with pytest.raises(StorageLimitError):
            f.write(b'4')
    assert guard.usage()['temp_bytes'] == 3


def test_path_escape_and_symlink(guard, tmp_path):
    with pytest.raises(StorageLimitError):
        guard.open('../outside')
    guard.root.mkdir()
    (guard.root / 'link').symlink_to(tmp_path)
    with pytest.raises(StorageLimitError):
        guard.open('link/outside')


def test_preserve_existing_files(guard):
    with guard.open('x') as f:
        f.write(b'original')
    with pytest.raises(FileExistsError):
        guard.open('x')


def test_project_exact_fields_no_assets():
    result = list(project_stream(io.BytesIO(body(step())), 'word'))
    assert tuple(result[0]) == FIELDS
    assert json.loads(result[0]['args']) == {'button': 'left', 'nested': {'text': 'ok'}}
    serialized = json.dumps(result)
    assert 'invalid/image' not in serialized and 'accessibility' not in serialized
    assert 'thought' not in serialized and 'coordinate_x' not in serialized


def test_json_string_args_sanitized():
    d = step()
    d['step']['action']['args'] = json.dumps({'text': 'hello', 'ui_tree': {'bad': 1}})
    row = next(project_stream(io.BytesIO(body(d)), 'word'))
    assert json.loads(row['args']) == {'text': 'hello'}


@pytest.mark.parametrize('action,expected', [
    ({'control_test': 'File Tab'}, 'File Tab'),
    ({'control_text': 'correct', 'control_test': 'legacy'}, 'correct'),
    ({'control_test': 'legacy', 'control_text': 'correct'}, 'correct'),
    ({'control_test': 'legacy', 'control_text': None}, 'legacy'),
])
def test_published_control_test_alias(action, expected):
    d = step();d['step']['action'] = action
    row = next(project_stream(io.BytesIO(body(d)), 'word'))
    assert row['control_text'] == expected
    assert 'control_test' not in row


@pytest.mark.parametrize('mutation', ['app', 'id', 'step_id'])
def test_invalid_record_rejected(mutation):
    d = step()
    if mutation == 'app':
        d['app_domain'] = 'excel'
    elif mutation == 'id':
        del d['execution_id']
    else:
        d['step_id'] = True
    with pytest.raises(ValueError):
        list(project_stream(io.BytesIO(body(d)), 'word'))


def test_bounded_file_like_no_read_all():
    calls = []
    reader = BoundedReader(io.BytesIO(b'123456'), 5, calls.append)
    with pytest.raises(ValueError):
        reader.read()
    assert reader.read(3) == b'123'
    with pytest.raises(ValueError):
        reader.read(3)
    assert sum(calls) == 6


def test_small_reads_supported():
    class Small(io.BytesIO):
        def read(self, n=-1):
            assert n >= 0
            return super().read(min(n, 7))
    assert len(list(project_stream(Small(body(step(), step(index=1))), 'word'))) == 2


class FakeRemote(HFRemote):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = payloads

    @contextlib.contextmanager
    def open_jsonl(self, path, revision, approved_paths):
        allowed_path(path)
        assert path in approved_paths
        self.data_paths.append(path)
        yield BoundedReader(io.BytesIO(self.payloads[path]), 256 * MiB, self._data_count)


def plan_for(payloads, cap=2000):
    return {'repo': 'vyokky/GUI-360', 'revision': REV, 'max_trajectories_per_app': cap,
            'files': [{'path': p, 'app': allowed_path(p), 'revision': REV, 'size_bytes': len(b)}
                      for p, b in payloads.items()]}


def test_2000_trajectories_per_app_and_parquet_projection(guard):
    payloads = {PREFIXES[app] + '/sample.jsonl': body(*(step(app, f't{i}') for i in range(2001))) for app in APPS}
    remote = FakeRemote(payloads)
    result = extract(remote, guard, plan_for(payloads))
    assert result['trajectories'] == dict.fromkeys(APPS, 2000)
    table = pq.read_table(guard.path('data/curated/gui360_office.parquet'))
    assert table.num_rows == 6000 and tuple(table.column_names) == FIELDS
    assert remote.media_downloads == dict.fromkeys(remote.media_downloads, 0)
    assert not list(guard.root.rglob('*.jsonl'))
    assert guard.usage()['temp_bytes'] == 0


def test_count_execution_ids_not_action_rows_and_stop_files(guard):
    payloads = {PREFIXES['word'] + '/a.jsonl': body(step(index=0), step(index=1)),
                PREFIXES['word'] + '/b.jsonl': body(step(execution='t2')),
                PREFIXES['word'] + '/c.jsonl': body(step(execution='t3'))}
    remote = FakeRemote(payloads)
    result = extract(remote, guard, plan_for(payloads, 2), cap=2)
    assert result['trajectories']['word'] == 2 and result['action_rows'] == 3
    assert len(remote.data_paths) == 2


def test_malformed_stream_never_publishes_final(guard):
    payloads = {PREFIXES['word'] + '/a.jsonl': body(step()) + b'{malformed'}
    with pytest.raises(Exception):
        extract(FakeRemote(payloads), guard, plan_for(payloads))
    assert not guard.path('data/curated/gui360_office.parquet').exists()


def test_empty_dataset_has_schema_and_zero_counts(guard):
    result = extract(FakeRemote({}), guard, plan_for({}))
    assert result['action_rows'] == 0
    assert tuple(pq.read_schema(guard.path('data/curated/gui360_office.parquet')).names) == FIELDS


def test_plan_tampering_changes_hash():
    p = plan_for({PREFIXES['word'] + '/a.jsonl': b''})
    before = digest(p)
    p['files'][0]['path'] = 'train/image/b.png'
    assert digest(p) != before
    with pytest.raises(ValueError):
        validate_plan(p)


def test_redirect_to_media_host_rejected():
    class Response:
        status_code = 302
        headers = {'Location': 'https://evil.example/image.png'}
        def close(self):
            pass
    class Session:
        def get(self, *args, **kwargs):
            return Response()
    remote = HFRemote(Session())
    with pytest.raises(ValueError, match='Untrusted'):
        remote._get('https://huggingface.co/resolve', data=True)


def test_inventory_only_queries_exact_directories():
    calls = []
    remote = HFRemote()
    def metadata(url):
        calls.append(url)
        app = next(a for a in APPS if '/' + a + '/' in url)
        return [{'type': 'file', 'path': PREFIXES[app] + '/a.jsonl', 'size': 9},
                {'type': 'directory', 'path': 'train/image'},
                {'type': 'file', 'path': 'processed_data/x.jsonl', 'size': 999}], None
    remote.metadata = metadata
    assert len(remote.inventory(REV)) == 3
    assert len(calls) == 3 and all('recursive=false' in u and '/in_app/success?' in u for u in calls)


def test_pagination_cannot_escape():
    remote = HFRemote()
    remote.metadata = lambda _: ([], 'https://huggingface.co/api/datasets/vyokky/GUI-360/tree/main/image')
    with pytest.raises(ValueError, match='escaped'):
        remote.inventory(REV)


def test_plan_does_not_open_remote_data(guard):
    remote = FakeRemote({})
    remote.resolve_revision = lambda: REV
    remote.inventory = lambda _: [{'app': app, 'path': PREFIXES[app] + '/a.jsonl', 'size_bytes': 10,
                                   'revision': REV, 'oid': ''} for app in APPS]
    p = make_plan(guard, remote)
    assert p['planned_file_counts'] == dict.fromkeys(APPS, 1)
    assert remote.data_paths == []
    assert not guard.path('data/curated/gui360_office.parquet').exists()
    r = json.loads(guard.path('outputs/storage_report.json').read_text())
    assert not r['extraction_completed'] and r['remote_jsonl_bytes_read'] == 0
    assert r['workspace_bytes'] == guard.usage()['workspace_bytes']


def test_report_limits(guard):
    guard.root.mkdir()
    r = report(guard, FakeRemote({}), 'awaiting_path_review')
    save_report(guard, r)
    result = json.loads(guard.path('outputs/storage_report.json').read_text())
    assert all(result['invariants'].values())
    assert result['thresholds'] == {'min_free_bytes': 20 * GiB, 'max_workspace_bytes': 5 * GiB,
                                    'max_temp_bytes': 256 * MiB}


def test_guard_blocks_parquet_write_before_remote_access(guard):
    guard.disk_usage = lambda _: Usage(100 * GiB, 90 * GiB, 10 * GiB)
    remote = FakeRemote({PREFIXES['word'] + '/a.jsonl': body(step())})
    with pytest.raises(StorageLimitError):
        extract(remote, guard, plan_for(remote.payloads))
    assert remote.data_paths == []


def test_arguments_budget_rejects_large_payload():
    d = step()
    d['step']['action']['args'] = {'text': 'x' * 70000}
    with pytest.raises(ValueError, match='budget'):
        list(project_stream(io.BytesIO(body(d)), 'word'))


def test_hf_media_redirect_blocked_before_second_get():
    calls = []
    class Response:
        status_code = 302
        headers = {'Location': 'https://huggingface.co/image/x.png'}
        def close(self):
            pass
    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            return Response()
    remote = HFRemote(Session())
    with pytest.raises(ValueError, match='Media'):
        remote._get('https://huggingface.co/resolve/file.jsonl', data=True)
    assert len(calls) == 1


def test_connection_retry_is_bounded(monkeypatch):
    import requests
    import wps_operation_dataset.remote as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    calls = []
    class Session:
        def get(self, *args, **kwargs):
            calls.append(1)
            raise requests.ConnectionError('transient proxy failure')
    with pytest.raises(requests.ConnectionError):
        HFRemote(Session())._get('https://huggingface.co/resolve/test.jsonl', data=True)
    assert len(calls) == 3


@pytest.mark.parametrize('fail', [False, True])
def test_schema_repair_atomic_and_audited(guard, monkeypatch, fail):
    import wps_operation_dataset.repair_schema as repair
    payloads = {PREFIXES[a] + '/a.jsonl': body(step(a)) for a in APPS}
    plan = plan_for(payloads)
    result = extract(FakeRemote(payloads), guard, plan)
    guard.write_json('outputs/inventory/access_plan.json', plan)
    save_report(guard, report(guard, FakeRemote(payloads), 'complete', result))
    original = guard.path('data/curated/gui360_office.parquet').read_bytes()
    if fail:
        payloads = {p: b'{bad' for p in payloads}
    monkeypatch.chdir(guard.root)
    monkeypatch.setattr(repair, 'HFRemote', lambda: FakeRemote(payloads))
    monkeypatch.setattr(repair, 'digest', lambda _: '20a14a1135d8ff86649b274f20e1b574a71903b7ca7e63e3321c033c9946a751')
    monkeypatch.setattr(repair.time, 'sleep', lambda _: None)
    if fail:
        with pytest.raises(RuntimeError):
            repair.main()
        assert guard.path('data/curated/gui360_office.parquet').read_bytes() == original
    else:
        repair.main()
        s = json.loads(guard.path('outputs/storage_report.json').read_text())
        assert s['schema_repair_complete']
        assert len(s['remote_jsonl_paths_requested']) == 3
