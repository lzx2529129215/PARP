"""Per-process collection timings; disabled unless an explicit output directory is set."""
import atexit,functools,json,os,threading,time
from pathlib import Path
_folder=os.environ.get('PARP_COLLECTION_PROFILE_DIR','');_lock=threading.RLock();_rows={};_last=0.
def flush(force=False):
 global _last
 if not _folder:return
 with _lock:
  now=time.monotonic()
  if not force and now-_last<2:return
  p=Path(_folder);p.mkdir(parents=True,exist_ok=True)
  target=p/(str(os.getpid())+'.json');temporary=target.with_suffix('.tmp')
  temporary.write_text(json.dumps(dict(pid=os.getpid(),stamp_ns=time.monotonic_ns(),rows=_rows)))
  os.replace(temporary,target);_last=now
def profiled(name):
 def decorate(function):
  if not _folder:return function
  @functools.wraps(function)
  def wrapped(*args,**kwargs):
   cpu=time.thread_time_ns();wall=time.monotonic_ns()
   try:return function(*args,**kwargs)
   finally:
    elapsed_cpu=time.thread_time_ns()-cpu;elapsed_wall=time.monotonic_ns()-wall
    with _lock:
     row=_rows.setdefault(name,dict(calls=0,cpu_ns=0,wall_ns=0));row['calls']+=1;row['cpu_ns']+=elapsed_cpu;row['wall_ns']+=elapsed_wall
    flush()
  return wrapped
 return decorate
atexit.register(lambda:flush(True))
