import json,collections
from pathlib import Path
from urllib.parse import urlparse
r=Path(__file__).parent/'Reduced-A11y-CUA/SU'
apps=collections.Counter(); titles=collections.defaultdict(collections.Counter); hosts=collections.Counter();typ=collections.Counter(); meta=0;events=0
for p in r.rglob('*.json'):
 if 'a11y_tree' in p.name:continue
 d=json.loads(p.read_text())
 if p.name.startswith('metadata_'):meta+=1;continue
 if isinstance(d,list):
  for e in d:
   w=e.get('window',{});a=w.get('application','?');apps[a]+=1;titles[a][w.get('title','')]+=1;events+=1
 elif 'web_logs' in str(p):
  hosts[urlparse(d.get('url','')).netloc]+=1
  typ.update(e.get('type','?') for e in d.get('interactions',[]))
print('METADATA',meta,'EVENTS',events);print('APPS',apps);print('HOSTS',hosts); print('WEB TYPES',typ)
for a,t in titles.items():print(a,t.most_common(45))
