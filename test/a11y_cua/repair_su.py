import requests,json,time,concurrent.futures,threading
from pathlib import Path
B=Path(__file__).parent;j=json.loads((B/'repository.json').read_text());root=B/'Reduced-A11y-CUA';local=threading.local()
def fetch(n):
 try:
  if not hasattr(local,'s'):local.s=requests.Session()
  r=local.s.get(f"https://huggingface.co/datasets/berkeley-hci/Reduced-A11y-CUA/resolve/{j['sha']}/{n}",timeout=(10,45));r.raise_for_status();p=root/n;p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.repair');tmp.write_bytes(r.content);tmp.replace(p);return 1
 except Exception:return 0
for turn in range(15):
 ns=[x['rfilename'] for x in j['siblings'] if x['rfilename'].startswith('SU/') and not (root/x['rfilename']).exists()];ns.sort(key=lambda n:('a11y_tree' in n or n.endswith('.html'),n));print('round',turn,'missing',len(ns),flush=True)
 if not ns:break
 # First fix only early files failed by the primary downloader, avoiding active writes.
 if turn==0:ns=[n for n in ns if n<'SU/SU3/4']+[n for n in ns if n.endswith('.json') and 'a11y_tree' not in n]
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
  for i,v in enumerate(ex.map(fetch,ns),1):
   if i%100==0:print('repair',i,flush=True)
 time.sleep(3)
