import contextlib
import io
import json
import re
import time
from urllib.parse import quote, urljoin, urlparse
import requests

REPO = 'vyokky/GUI-360'
APPS = ('word', 'excel', 'ppt')
PREFIXES = {app: f'train/data/{app}/in_app/success' for app in APPS}
PATH = re.compile(r'train/data/(word|excel|ppt)/in_app/success/[^/\\%?#]+\.jsonl\Z')


def allowed_path(path):
    if not isinstance(path, str) or not PATH.fullmatch(path) or '..' in path.split('/'):
        raise ValueError('Remote path is outside the exact allowlist: ' + str(path))
    return PATH.fullmatch(path).group(1)


def revision_ok(revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('A pinned 40-character commit is required')


class BoundedReader(io.RawIOBase):
    """Streaming file-like reader, never spools/caches bodies to disk."""
    def __init__(self, raw, limit, counter):
        self.raw, self.limit, self.counter = raw, limit, counter
        self.count = 0

    def readable(self):
        return True

    def read(self, size=-1):
        if size < 0:
            raise ValueError('Unbounded remote reads are forbidden')
        if size == 0:
            return b''
        data = self.raw.read(min(size, self.limit - self.count + 1))
        self.count += len(data)
        self.counter(len(data))
        if self.count > self.limit:
            raise ValueError('Remote stream exceeds configured byte budget')
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class HFRemote:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.data_paths = []
        self.jsonl_bytes = 0
        self.metadata_bytes = 0
        self.media_downloads = {x: 0 for x in ('png', 'jpg', 'jpeg', 'mp4', 'wav')}
        self.request_audit = []

    def _get(self, url, data=False):
        original_path = urlparse(url).path
        for _ in range(6):
            parsed = urlparse(url)
            host = parsed.hostname or ''
            valid = (host == 'huggingface.co' or
                     (data and (host.endswith('.huggingface.co') or host.endswith('.hf.co'))))
            if parsed.scheme != 'https' or not valid or parsed.username:
                raise ValueError('Untrusted remote URL')
            if re.search(r'\.(?:png|jpe?g|gif|webp|mp4|wav|mp3)(?:$|/)', parsed.path, re.I):
                raise ValueError('Media asset requests are forbidden')
            if not data and parsed.path != original_path:
                raise ValueError('Metadata redirect escaped its API endpoint')
            self.request_audit.append({'kind': 'jsonl' if data else 'metadata',
                                       'host': host, 'path': parsed.path})
            for attempt in range(3):
                try:
                    r = self.session.get(url, stream=True, allow_redirects=False, timeout=(15, 60),
                                         headers={'Accept-Encoding': 'identity'})
                    break
                except (requests.ConnectionError, requests.Timeout):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get('Location', '')
                r.close()
                url = urljoin(url, location)
                continue
            try:
                r.raise_for_status()
            except Exception:
                r.close()
                raise
            return r
        raise ValueError('Too many redirects')

    def metadata(self, url):
        with self._get(url) as r:
            reader = BoundedReader(r.raw, 4 * 1024 ** 2, self._meta_count)
            chunks = []
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return json.loads(b''.join(chunks)), r.links.get('next', {}).get('url')

    def _meta_count(self, n):
        self.metadata_bytes += n

    def _data_count(self, n):
        self.jsonl_bytes += n

    def resolve_revision(self):
        refs, _ = self.metadata(f'https://huggingface.co/api/datasets/{REPO}/refs')
        revision = next(x['targetCommit'] for x in refs['branches'] if x['name'] == 'main')
        revision_ok(revision)
        return revision

    def inventory(self, revision):
        revision_ok(revision)
        rows = []
        for app, prefix in PREFIXES.items():
            base = f'https://huggingface.co/api/datasets/{REPO}/tree/{revision}/{prefix}'
            url = base + '?recursive=false&limit=1000'
            visited = set()
            while url:
                if url in visited or len(visited) >= 100:
                    raise ValueError('Invalid or excessive inventory pagination')
                u = urlparse(url)
                if u.scheme != 'https' or u.netloc != 'huggingface.co' or u.path != urlparse(base).path:
                    raise ValueError('Pagination escaped the allowed inventory directory')
                visited.add(url)
                items, url = self.metadata(url)
                for item in items:
                    if item['type'] != 'file':
                        continue  # Never recurse into any child directory.
                    path = item['path']
                    try:
                        path_app = allowed_path(path)
                    except ValueError:
                        continue
                    if path_app != app:
                        raise ValueError('Inventory application mismatch')
                    rows.append(dict(app=app, path=path, size_bytes=int(item['size']),
                                     revision=revision, oid=item.get('oid', '')))
        if len({r['path'] for r in rows}) != len(rows):
            raise ValueError('Duplicate inventory paths')
        return sorted(rows, key=lambda r: (APPS.index(r['app']), r['path']))

    @contextlib.contextmanager
    def open_jsonl(self, path, revision, approved_paths):
        allowed_path(path)
        revision_ok(revision)
        if path not in approved_paths:
            raise ValueError('Path absent from the reviewed plan')
        url = f'https://huggingface.co/datasets/{REPO}/resolve/{revision}/{quote(path, safe="/")}'
        self.data_paths.append(path)
        with self._get(url, data=True) as r:
            content_type = r.headers.get('Content-Type', '').lower()
            if any(x in content_type for x in ('image/', 'audio/', 'video/', 'text/html')):
                raise ValueError('Unexpected non-JSONL response')
            yield BoundedReader(r.raw, 256 * 1024 ** 2, self._data_count)
