import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from .remote import APPS, PREFIXES, REPO, HFRemote, allowed_path, revision_ok
from .storage import DiskGuard, GiB


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_plan(plan):
    if plan['repo'] != REPO or not 1 <= plan['max_trajectories_per_app'] <= 2000:
        raise ValueError('Unexpected repository or cap')
    revision_ok(plan['revision'])
    seen = set()
    for f in plan['files']:
        if allowed_path(f['path']) != f['app'] or f['revision'] != plan['revision']:
            raise ValueError('Plan contains mismatched application/revision')
        if f['path'] in seen or f['size_bytes'] < 0:
            raise ValueError('Duplicate or invalid file')
        seen.add(f['path'])


def report(guard, remote, state, result=None):
    result = result or {'trajectories': dict.fromkeys(APPS, 0), 'action_rows': 0, 'files_read': 0}
    storage = guard.check()
    return dict(state=state, extraction_completed=state == 'complete', **storage, **result,
                thresholds={'min_free_bytes': guard.min_free, 'max_workspace_bytes': guard.max_workspace,
                            'max_temp_bytes': guard.max_temp},
                downloaded=remote.media_downloads, remote_jsonl_bytes_read=remote.jsonl_bytes,
                remote_metadata_bytes_read=remote.metadata_bytes, remote_jsonl_paths_requested=remote.data_paths,
                request_audit=remote.request_audit,
                auxiliary_asset_requests=0, persisted_accessibility_trees=0, persisted_raw_jsonl_files=0,
                inline_content_note='Remote JSONL can embed screenshot/a11y fields: transport bytes may contain these fields, but they are not materialized as records, persisted, or fetched as separate assets.',
                invariants={'trajectory_caps': all(n <= 2000 for n in result['trajectories'].values()),
                            'no_media_asset_requests': all(n == 0 for n in remote.media_downloads.values()),
                            'workspace_within_limit': storage['workspace_bytes'] <= guard.max_workspace,
                            'free_space_above_minimum': storage['remaining_free_bytes'] >= guard.min_free,
                            'temp_within_limit': storage['temp_bytes'] <= guard.max_temp})


def save_report(guard, data):
    # Include the report itself in accounting; stabilize its serialized size.
    name = 'outputs/storage_report.json'
    temp = '.tmp/storage_report.json'
    existing = guard.path(name)
    old_size = existing.stat().st_size if existing.exists() else 0
    baseline = guard.usage()
    for _ in range(6):
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
        data['workspace_bytes'] = baseline['workspace_bytes'] - old_size + len(payload)
        data['remaining_free_bytes'] = baseline['remaining_free_bytes'] - max(0, len(payload) - old_size)
    with guard.open(temp) as f:
        f.write((json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
    guard.commit(temp, name)
    guard.check()


def make_plan(guard, remote):
    guard.check()
    revision = remote.resolve_revision()
    files = remote.inventory(revision)
    # Select at most 2000 files/app initially. Each file may contain many steps.
    # Extraction counts execution_ids, not paths or rows; never exceeds 2000/app.
    selected = []
    for app in APPS:
        selected.extend([f for f in files if f['app'] == app][:2000])
    plan = {'repo': REPO, 'revision': revision, 'max_trajectories_per_app': 2000,
            'allowed_patterns': [prefix + '/*.jsonl' for prefix in PREFIXES.values()],
            'files': selected,
            'inventory_file_counts': {app: sum(f['app'] == app for f in files) for app in APPS},
            'planned_file_counts': {app: sum(f['app'] == app for f in selected) for app in APPS},
            'planned_max_file_bytes': sum(f['size_bytes'] for f in selected),
            'selection': 'Lexicographically first up to 2000 files/app; stop opening files once 2000 execution_ids reached. Not a representative random sample.'}
    validate_plan(plan)
    selected_paths = {f['path'] for f in selected}
    buf = io.StringIO()
    fields = ['app', 'path', 'size_bytes', 'revision', 'oid', 'planned']
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for f in files:
        writer.writerow(dict(f, planned=f['path'] in selected_paths))
    with guard.open('outputs/inventory/gui360_files.csv') as out:
        out.write(buf.getvalue().encode())
    guard.write_json('outputs/inventory/access_plan.json', plan)
    save_report(guard, report(guard, remote, 'awaiting_path_review'))
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan', help='Metadata only; does not open remote JSONL')
    e = sub.add_parser('extract')
    e.add_argument('--reviewed-plan-sha256', required=True)
    args = parser.parse_args()
    guard, remote = DiskGuard(args.workspace), HFRemote()
    guard.check()
    if args.command == 'plan':
        plan = make_plan(guard, remote)
        print(json.dumps({'allowed_paths': plan['allowed_patterns'], 'inventory_file_counts': plan['inventory_file_counts'],
                          'planned_file_counts': plan['planned_file_counts'], 'planned_max_file_gib': plan['planned_max_file_bytes']/GiB,
                          'current_disk': guard.check(), 'reviewed_plan_sha256': digest(plan),
                          'status': 'No remote trajectory content read; awaiting user review.'}, ensure_ascii=False, indent=2))
        return
    plan = json.loads(guard.path('outputs/inventory/access_plan.json').read_text())
    validate_plan(plan)
    if args.reviewed_plan_sha256 != digest(plan):
        parser.error('Reviewed plan hash does not match; no remote data accessed')
    from .extract import extract
    try:
        result = extract(remote, guard, plan)
        save_report(guard, report(guard, remote, 'complete', result))
    except Exception:
        # Try to persist failure metadata only if quota still allows writing.
        try:
            save_report(guard, report(guard, remote, 'failed'))
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
