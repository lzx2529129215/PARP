"""Resume the complete fixed-revision reduced dataset; retry transient proxy errors."""
import requests,json,time,concurrent.futures,threading,hashlib,re
from pathlib import Path
BASE=Path(__file__).parent;ROOT=BASE/'Reduced-A11y-CUA';ROOT.mkdir(exist_ok=True)
if (BASE/'repository.json').exists():info=json.loads((BASE/'repository.json').read_text())
else:
 r=requests.get('https://huggingface.co/api/datasets/berkeley-hci/Reduced-A11y-CUA',timeout=60);r.raise_for_status();info=r.json();(BASE/'repository.json').write_text(json.dumps(info,indent=2))
expected_lfs={x['path']:x for x in json.loads((BASE/'git_integrity.json').read_text())['lfs_files']} if (BASE/'git_integrity.json').exists() else {}
rev=info['sha'];names=[x['rfilename'] for x in info['siblings']];local=threading.local();start=time.time()
rate_lock=threading.Lock();next_request=0.;cooldown_until=0.
def throttle():
 global next_request
 while True:
  with rate_lock:
   now=time.time();wait=max(next_request,cooldown_until)-now
   if wait<=0:next_request=now+.15;return
  time.sleep(min(wait,5))
def fetch(n):
 global cooldown_until
 p=ROOT/n
 if p.exists():return None
 try:
  if not hasattr(local,'s'):local.s=requests.Session()
  throttle()
  r=local.s.get(f'https://huggingface.co/datasets/berkeley-hci/Reduced-A11y-CUA/resolve/{rev}/{n}',timeout=(10,60))
  if r.status_code==429:
   match=re.search(r't=(\d+)',r.headers.get('RateLimit',''));delay=int(match[1])+5 if match else 305
   with rate_lock:cooldown_until=max(cooldown_until,time.time()+delay)
   print('rate limit cooldown',delay,flush=True)
  r.raise_for_status()
  expected=expected_lfs.get(n)
  if expected and (len(r.content)!=expected['bytes'] or hashlib.sha256(r.content).hexdigest()!=expected['sha256']):raise ValueError('LFS size/SHA256 mismatch; response rejected')
  p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.partial');tmp.write_bytes(r.content);tmp.replace(p);return None
 except Exception as e:return str(e)
for turn in range(50):
 missing=[n for n in names if not (ROOT/n).exists()];missing.sort(key=lambda n:(not n.startswith('SU/'),n));print('round',turn,'missing',len(missing),flush=True)
 if not missing:break
 errors=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
  futures={ex.submit(fetch,n):n for n in missing}
  for i,f in enumerate(concurrent.futures.as_completed(futures),1):
   error=f.result()
   if error:errors.append((futures[f],error))
   if i%100==0:print(json.dumps({'round':turn,'processed':i,'queued':len(missing),'errors':len(errors),'seconds':round(time.time()-start)}),flush=True)
 (BASE/'download_errors.json').write_text(json.dumps(errors,indent=2));time.sleep(2)
missing=[n for n in names if not (ROOT/n).exists()]
if missing:raise RuntimeError(f'{len(missing)} missing files')
results=[]
for n in names:
 p=ROOT/n;h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 results.append({'path':n,'bytes':p.stat().st_size,'sha256':h.hexdigest()})
(BASE/'download_manifest.json').write_text(json.dumps({'revision':rev,'files':results,'missing':[]},indent=2));print('COMPLETE',sum(r['bytes'] for r in results),'bytes',flush=True)
