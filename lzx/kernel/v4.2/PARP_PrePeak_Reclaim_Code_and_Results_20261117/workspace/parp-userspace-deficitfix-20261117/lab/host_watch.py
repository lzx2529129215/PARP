"""Host-memory gate for this diagnostic cohort. Ends on HOST_STOP or in 2h."""
import ctypes
import json
from pathlib import Path
import time

HERE=Path(__file__).resolve().parent


class Memory(ctypes.Structure):
    _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[
        (name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available',
                                            'virtual_total','virtual_available','extended_available')]


def publish(row,directory=HERE,*,attempts=25,delay_s=.02):
    """A guest reader may temporarily deny replacing an HGFS-backed file."""
    directory=Path(directory);temporary=directory/'host-latest.tmp'
    temporary.write_text(json.dumps(row))
    for attempt in range(attempts):
        try:
            temporary.replace(directory/'host-latest.json');return True
        except PermissionError:
            if attempt+1<attempts:time.sleep(delay_s)
    # Keep the last complete snapshot; admission will wait if it becomes stale.
    return False


def main():
    until=time.monotonic()+7200
    if (HERE/'HOST_STOP').exists():raise SystemExit('existing stop marker; do not silently restart')
    with (HERE/'host-memory.jsonl').open('a') as output:
        first=True
        while time.monotonic()<until and not (HERE/'HOST_STOP').exists():
            memory=Memory();memory.length=ctypes.sizeof(memory)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                raise ctypes.WinError()
            row=dict(wall_s=time.time(),available_bytes=memory.available,total_bytes=memory.total,load=memory.load)
            published=publish(row)
            if not published:print('HOST_PUBLISH_BUSY; retaining last complete snapshot',flush=True)
            output.write(json.dumps(row)+'\n');output.flush()
            if first:
                print('HOST_WATCH_READY available_GiB=%.2f'%(memory.available/1024**3),flush=True)
                first=False
            time.sleep(2)


if __name__=='__main__':main()
