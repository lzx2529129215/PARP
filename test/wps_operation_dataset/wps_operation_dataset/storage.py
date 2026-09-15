import io
import json
import os
from pathlib import Path
import shutil
import threading

GiB = 1024 ** 3
MiB = 1024 ** 2


class StorageLimitError(RuntimeError):
    pass


class DiskGuard:
    def __init__(self, workspace, min_free_gb=20, max_workspace_gb=5, max_temp_mb=256,
                 disk_usage=shutil.disk_usage):
        self.root = Path(workspace).absolute()
        if self.root.is_symlink():
            raise StorageLimitError('Workspace cannot be a symlink')
        self.min_free = int(min_free_gb * GiB)
        self.max_workspace = int(max_workspace_gb * GiB)
        self.max_temp = int(max_temp_mb * MiB)
        self.disk_usage = disk_usage
        self.lock = threading.RLock()

    def path(self, relative):
        path = self.root / relative
        if not path.absolute().is_relative_to(self.root) or '..' in path.parts:
            raise StorageLimitError('Path escapes workspace')
        for part in (path, *path.parents):
            if part == self.root.parent:
                break
            if part.is_symlink():
                raise StorageLimitError('Symlink not permitted: ' + str(part))
        return path

    def usage(self):
        total = temp = 0
        if self.root.exists():
            for parent, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    p = Path(parent) / name
                    if p.is_symlink():
                        raise StorageLimitError('Workspace contains a symlink: ' + str(p))
                for name in files:
                    p = Path(parent) / name
                    size = p.stat().st_size
                    total += size
                    if p.is_relative_to(self.root / '.tmp'):
                        temp += size
        probe = self.root if self.root.exists() else self.root.parent
        return {'workspace_bytes': total, 'temp_bytes': temp,
                'remaining_free_bytes': self.disk_usage(probe).free}

    def check(self, incoming_bytes=0, temporary=False):
        if incoming_bytes < 0:
            raise ValueError('Negative write reservation')
        state = self.usage()
        if state['remaining_free_bytes'] - incoming_bytes < self.min_free:
            raise StorageLimitError('Free disk would fall below 20 GiB / configured minimum')
        if state['workspace_bytes'] + incoming_bytes > self.max_workspace:
            raise StorageLimitError('Workspace quota exceeded')
        if state['temp_bytes'] + (incoming_bytes if temporary else 0) > self.max_temp:
            raise StorageLimitError('Temporary storage quota exceeded')
        return state

    def open(self, relative):
        with self.lock:
            p = self.path(relative)
            self.check()
            p.parent.mkdir(parents=True, exist_ok=True)
            self.check()
            return GuardedWriter(self, p)

    def write_json(self, relative, value):
        with self.open(relative) as f:
            f.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())

    def commit(self, source, destination):
        with self.lock:
            src, dst = self.path(source), self.path(destination)
            self.check()
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.check()
            os.replace(src, dst)


class GuardedWriter(io.RawIOBase):
    def __init__(self, guard, path):
        self.guard, self.path = guard, path
        self.file = path.open('xb', buffering=0)  # Preserve previous results.

    def writable(self):
        return True

    def tell(self):
        return self.file.tell()

    def write(self, data):
        with self.guard.lock:
            self.guard.check(len(data), self.path.is_relative_to(self.guard.root / '.tmp'))
            view = memoryview(data)
            size = len(view)
            while view:
                n = self.file.write(view)
                if not n:
                    raise OSError('Short write')
                view = view[n:]
            return size

    def flush(self):
        if not self.file.closed:
            self.file.flush()

    def close(self):
        if not self.closed:
            super().close()
            self.file.close()
