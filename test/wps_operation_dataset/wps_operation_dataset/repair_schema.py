"""Bounded reread of an already-extracted, reviewed plan after schema correction."""
import collections
import concurrent.futures
import hashlib
import json
import time
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from .storage import DiskGuard
from .remote import HFRemote
from .extract import project_stream, SCHEMA
from .cli import digest, save_report, validate_plan


def main():
    root = Path.cwd()
    guard = DiskGuard(root)
    guard.check()
    plan = json.loads(guard.path('outputs/inventory/access_plan.json').read_text())
    validate_plan(plan)
    expected = '20a14a1135d8ff86649b274f20e1b574a71903b7ca7e63e3321c033c9946a751'
    if digest(plan) != expected:
        raise ValueError('Not the reviewed plan for this schema repair')
    approved = {f['path'] for f in plan['files']}
    stats = json.loads(guard.path('outputs/storage_report.json').read_text())
    target = guard.path('data/curated/gui360_office.parquet')
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    def fetch(item):
        remote = HFRemote()
        for attempt in range(3):
            rows, size = [], 0
            try:
                with remote.open_jsonl(item['path'], plan['revision'], approved) as stream:
                    for row in project_stream(stream, item['app']):
                        rows.append(row)
                        size += sum(len(x.encode()) for x in row.values() if isinstance(x, str))
                        if len(rows) > 4096 or size > 4 * 1024 ** 2:
                            raise ValueError('Projected file exceeds repair memory budget')
                return item, rows, remote, None
            except ValueError as exc:
                return item, [], remote, str(exc)
            except Exception as exc:
                if attempt == 2:
                    return item, [], remote, str(exc)
                time.sleep(2 ** attempt)

    ids = {app: set() for app in ('word', 'excel', 'ppt')}
    count = done = 0
    error = None
    temp = '.tmp/gui360_schema_repaired.parquet.part'
    try:
        with guard.open(temp) as sink:
            with pq.ParquetWriter(sink, SCHEMA, compression='zstd') as writer:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    items, pending = iter(plan['files']), collections.deque()
                    for _ in range(3):
                        item = next(items, None)
                        if item:
                            pending.append(pool.submit(fetch, item))
                    while pending:
                        item, rows, remote, failure = pending.popleft().result()
                        stats['remote_jsonl_bytes_read'] += remote.jsonl_bytes
                        stats['remote_jsonl_paths_requested'] += remote.data_paths
                        stats['request_audit'] += remote.request_audit
                        if failure:
                            error = failure
                        if not error:
                            batch = []
                            for row in rows:
                                app, ident = row['app_domain'], row['execution_id']
                                if ident not in ids[app] and len(ids[app]) >= 2000:
                                    continue
                                ids[app].add(ident)
                                batch.append(row)
                            if batch:
                                writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                                count += len(batch)
                        done += 1
                        if done % 10 == 0:
                            print(f'Schema repair {done}/{len(plan["files"])}', flush=True)
                        item = next(items, None) if not error else None
                        if item:
                            pending.append(pool.submit(fetch, item))
        if error:
            raise RuntimeError(error)
        table = pq.read_table(guard.path(temp))
        assert table.num_rows == stats['action_rows']
        assert table['control_text'].null_count < table.num_rows
        assert {app: len(values) for app, values in ids.items()} == stats['trajectories']
        assert hashlib.sha256(target.read_bytes()).hexdigest() == old_hash
        guard.commit(temp, 'data/curated/gui360_office.parquet')
        stats['schema_compatibility'] = dict(input_alias='step.action.control_test', output='control_text',
                                           prior_parquet_sha256=old_hash, repair_files_read=done,
                                           parallelism=3, max_projected_memory_per_file_bytes=4 * 1024 ** 2)
        stats['schema_repair_complete'] = True
        print('REPAIR COMPLETE', count, 'rows; control_text nulls', table['control_text'].null_count, flush=True)
    finally:
        save_report(guard, stats)


if __name__ == '__main__':
    main()
