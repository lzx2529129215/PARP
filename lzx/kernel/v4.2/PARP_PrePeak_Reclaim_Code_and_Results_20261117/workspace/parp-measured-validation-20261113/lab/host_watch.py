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
            temporary=HERE/'host-latest.tmp'
            temporary.write_text(json.dumps(row))
            temporary.replace(HERE/'host-latest.json')
            output.write(json.dumps(row)+'\n');output.flush()
            if first:
                print('HOST_WATCH_READY available_GiB=%.2f'%(memory.available/1024**3),flush=True)
                first=False
            time.sleep(2)


if __name__=='__main__':main()
