import json
import re
import ijson
import pyarrow as pa
import pyarrow.parquet as pq

FIELDS = ('execution_id', 'app_domain', 'request', 'step_id', 'action_type',
          'control_text', 'control_label', 'function', 'args', 'status')
SCHEMA = pa.schema([(key, pa.int64() if key == 'step_id' else pa.string()) for key in FIELDS])
BLOCKED_KEY = re.compile(r'image|screenshot|audio|video|base64|accessibility|a11y|ui_tree|control_infos|uia_controls', re.I)


def text(value, limit=16384):
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError('Expected scalar text')
    value = str(value)
    if value.lower().startswith(('data:image/', 'data:audio/', 'data:video/')):
        return '[omitted inline media]'
    if len(value) > limit:
        raise ValueError('Text field exceeds safe size limit')
    return value


def safe_args(value, depth=0):
    if depth > 16:
        raise ValueError('Arguments nesting exceeds safe limit')
    if isinstance(value, dict):
        return {k: safe_args(v, depth + 1) for k, v in value.items() if not BLOCKED_KEY.search(k)}
    if isinstance(value, list):
        return [safe_args(v, depth + 1) for v in value]
    if isinstance(value, str):
        return text(value, 8192)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ValueError('Unsupported argument type')


def project_stream(file_like, expected_app):
    """Parse events incrementally; never build raw records, screenshots or UI trees."""
    record = None
    builder = None
    args_depth = args_events = args_bytes = 0
    prefixes = {key: key for key in ('execution_id', 'app_domain', 'request', 'step_id')}
    prefixes.update({f'step.action.{k}': k for k in ('action_type', 'control_text', 'control_label', 'function')})
    # The pinned release spells this field control_test, unlike its README.
    prefixes['step.action.control_test'] = 'control_text'
    prefixes['step.status'] = 'status'
    for prefix, event, value in ijson.parse(file_like, multiple_values=True, use_float=True, buf_size=16384):
        if prefix == '' and event == 'start_map':
            record = {}
            continue
        if record is None:
            raise ValueError('Expected JSONL objects at the root')
        if prefix == 'step.action.args' and event in ('start_map', 'start_array'):
            builder = ijson.ObjectBuilder()
            args_depth = args_events = args_bytes = 0
        if builder is not None:
            args_events += 1
            args_bytes += len(value.encode()) if isinstance(value, str) else 8
            if args_events > 4096 or args_bytes > 65536:
                raise ValueError('Arguments exceed bounded projection budget')
            builder.event(event, value)
            args_depth += (event in ('start_map', 'start_array')) - (event in ('end_map', 'end_array'))
            if args_depth == 0:
                record['args'] = json.dumps(safe_args(builder.value), ensure_ascii=False, separators=(',', ':'))
                builder = None
            continue
        if prefix == 'step.action.args' and event in ('string', 'number', 'boolean', 'null'):
            # Some exporters encode the argument object as a JSON string.
            raw = text(value)
            if isinstance(value, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
            else:
                parsed = value
            record['args'] = json.dumps(safe_args(parsed), ensure_ascii=False, separators=(',', ':'))
        elif prefix in prefixes and event in ('string', 'number', 'boolean', 'null'):
            key = prefixes[prefix]
            if key == 'control_text' and record.get(key) is not None:
                if prefix.endswith('.control_test') or value is None:
                    continue
            if key == 'step_id':
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError('step_id must be a nonnegative integer')
                record[key] = value
            else:
                record[key] = text(value)
        elif prefix == '' and event == 'end_map':
            if not record.get('execution_id') or 'step_id' not in record:
                raise ValueError('Missing execution_id or step_id')
            if record.get('app_domain') != expected_app:
                raise ValueError('Record application does not match remote path')
            result = {k: record.get(k) for k in FIELDS}
            record = None  # Discard all input state before yielding.
            yield result


def extract(remote, guard, plan, cap=2000):
    if not 1 <= cap <= 2000:
        raise ValueError('Per-application trajectory cap must be 1..2000')
    if cap > plan['max_trajectories_per_app']:
        raise ValueError('Cannot exceed the reviewed cap')
    approved = {f['path'] for f in plan['files']}
    ids = {app: set() for app in ('word', 'excel', 'ppt')}
    rows_written = files_read = 0
    temporary = '.tmp/gui360_office.parquet.part'
    destination = 'data/curated/gui360_office.parquet'
    if guard.path(destination).exists():
        raise FileExistsError('Curated output already exists; preserve it or use a new workspace')
    # The sink checks quotas before every Arrow write, including headers/footer.
    with guard.open(temporary) as sink:
        with pq.ParquetWriter(sink, SCHEMA, compression='zstd') as writer:
            for f in plan['files']:
                app = f['app']
                if len(ids[app]) >= cap:
                    continue
                guard.check()
                with remote.open_jsonl(f['path'], plan['revision'], approved) as stream:
                    files_read += 1
                    batch = []
                    batch_bytes = 0
                    for row in project_stream(stream, app):
                        ident = row['execution_id']
                        if ident not in ids[app]:
                            if len(ids[app]) >= cap:
                                continue
                            ids[app].add(ident)
                        # Only projected fields survive this point.
                        batch.append(row)
                        batch_bytes += sum(len(v.encode()) for v in row.values() if isinstance(v, str))
                        if len(batch) >= 128 or batch_bytes >= 1024 ** 2:
                            writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                            rows_written += len(batch)
                            batch, batch_bytes = [], 0
                    if batch:
                        writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                        rows_written += len(batch)
    # No partial final dataset is published if parsing or writes fail.
    guard.commit(temporary, destination)
    return {'trajectories': {app: len(values) for app, values in ids.items()},
            'action_rows': rows_written, 'files_read': files_read}
