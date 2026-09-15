"""Independent audit of exported rows against sequence and fixed input files."""
import collections,csv,hashlib,json
from pathlib import Path
B=Path(__file__).parent;O=B/'results';read=lambda n:list(csv.DictReader((O/n).open()));j=json.loads((O/'statistics.json').read_text());s=j['totals'];sessions=read('sessions.csv');seq=read('interaction_sequence.csv');samples=read('reentry_samples.csv');events=read('merged_events.csv');switches=read('switches.csv');by=collections.defaultdict(list)
for r in seq:by[r['session']].append(r)
for sid,rs in by.items():
 assert all(float(a['start'])<=float(b['start']) and a['app_name']!=b['app_name'] for a,b in zip(rs,rs[1:]))
for r in samples:
 a=r['app_name'];leave=float(r['leave_time']);until=float(r['observed_until']);rs=by[r['session']];depart=[i for i,x in enumerate(rs) if float(x['start'])==leave and i and rs[i-1]['app_name']==a and x['app_name'] not in (a,'<UNKNOWN>')];assert depart,r
 future=rs[depart[0]+1:];boundary=next((float(x['start']) for x in future if x['app_name']=='<UNKNOWN>'),float('inf'));returns=[float(x['start']) for x in future if x['app_name']==a and float(x['start'])<boundary]
 if r['censored']=='0':assert returns and float(r['return_time'])==min(returns) and abs(float(r['reentry_s'])-(min(returns)-leave))<1e-6,r
 else:assert not [t for t in returns if t<=until],r
assert len(events)==s['merged_events'];assert len(switches)==s['raw_switches'];assert len(samples)==s['reentry_events']+s['censored_candidates'];assert len(sessions)==480;assert set(x['user'] for x in sessions)=={f'SU{i}' for i in range(1,9)}
for user in range(1,9):assert {int(x['task_id']) for x in sessions if x['user']==f'SU{user}'}==set(range(1,61))
files=read('su_file_hashes.csv')
for r in files:
 p=B/'Reduced-A11y-CUA'/r['path'];assert p.stat().st_size==int(r['bytes']);assert hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256']
result={'status':'PASS','sessions':480,'su_files_hashed':len(files),'events':len(events),'segments':len(seq),'candidate_episodes':len(samples),'tests':['8 users × exact tasks 1..60','chronological collapsed sequence','departure belongs to candidate app','next return independently located before unknown boundary','censored rows have no observed return','counts reconcile','SU input hashes unchanged']};(O/'validation.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
