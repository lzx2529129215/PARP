from common import *
from model import OrdinalReentry
from priority import risk_coldness
from models.app_lstm_visit_explicit import AppLSTMVisitExplicit,explicit_features
from models.app_lstm_visit_window import visit_probabilities
from models.segmented import LSTMSegmentedReentry
from build_dataset import labels
from evaluate import metric_rows
import csv,collections,datetime,torch
A11Y=ROOT.parents[2]/'test/a11y_cua';RAW=A11Y/'Reduced-A11y-CUA/SU'
PROCESS_MAP={'explorer.exe':'Files','winword.exe':'LibreOffice','excel.exe':'LibreOffice','powerpnt.exe':'LibreOffice','notepad.exe':'LibreOffice','onenote.exe':'LibreOffice','chrome.exe':'Falkon','msedge.exe':'Falkon','photos.exe':'Shotwell','mspaint.exe':'GIMP','vlc.exe':'VLC','wmplayer.exe':'VLC','taskmgr.exe':'SystemMonitor','slack.exe':'Pidgin'}

def sequences(kind):
 result=collections.defaultdict(list);stats=collections.Counter();names=set()
 if kind=='functional':
  for r in csv.DictReader((A11Y/'results/interaction_sequence.csv').open()):
   a=r['app_name'];result[r['session']].append(dict(t=float(r['start']),identity=a,proxy=VOCAB.get(a,31),end=float(r['end'])));names.add(a)
  stats['events']=sum(1 for _ in csv.DictReader((A11Y/'results/merged_events.csv').open()))
 else:
  for mp in sorted(RAW.rglob('metadata_*.json')):
   sid=str(mp.parent.relative_to(RAW));meta=json.loads(mp.read_text());events=[]
   for p in sorted(mp.parent.glob('*.json')):
    if 'a11y_tree' in p.name or p.name.startswith('metadata_'):continue
    d=json.loads(p.read_text())
    if not isinstance(d,list):continue
    for i,e in enumerate(d):
     a=(e.get('window') or {}).get('application','').lower();events.append((e['timestamp'],p.name,i,a));names.add(a)
   events.sort();stats['events']+=len(events);seq=[]
   for t,p,i,a in events:
    if not seq or seq[-1]['identity']!=a:seq.append(dict(t=t,identity=a,proxy=VOCAB.get(PROCESS_MAP.get(a,''),31),end=meta['session']['ended_at']))
   for i in range(len(seq)-1):seq[i]['end']=seq[i+1]['t']
   result[sid]=seq
 stats['source_identities']=len(names-{'<UNKNOWN>'});stats['sessions']=480;return result,dict(stats)

def build(kind):
 sessions,stats=sequences(kind);dest=OUT/'a11y'/kind;dest.mkdir(parents=True,exist_ok=True)
 rows=[dict(session=sid,**x) for sid,seq in sessions.items() for x in seq]
 with (dest/'sequence.csv').open('w') as f:
  writer=csv.DictWriter(f,fieldnames=['session','t','identity','proxy','end']);writer.writeheader();writer.writerows(rows)
 if kind=='process':
  native=[]
  for sid,seq in sessions.items():
   pending={}
   for i,x in enumerate(seq):
    if x['identity'] in pending:
     leave=pending.pop(x['identity']);native.append(dict(session=sid,identity=x['identity'],leave=leave,return_time=x['t'],interval_s=x['t']-leave,censored=0,observed_until=x['t']))
    if i:pending[seq[i-1]['identity']]=x['t']
   for ident,leave in pending.items():native.append(dict(session=sid,identity=ident,leave=leave,return_time=None,interval_s=None,censored=1,observed_until=seq[-1]['end']))
  with (dest/'native_reentry_episodes.csv').open('w') as f:
   writer=csv.DictWriter(f,fieldnames=['session','identity','leave','return_time','interval_s','censored','observed_until']);writer.writeheader();writer.writerows(native)
  observed=[r['interval_s'] for r in native if not r['censored']];stats['full_native_episodes']=len(native);stats['full_native_observed']=len(observed);stats['full_native_censored']=len(native)-len(observed);stats['full_native_return_cdf_counts']={str(int(t)):sum(v<=t for v in observed) for t in TH};stats['full_native_return_max_s']=max(observed) if observed else None
 identities=sorted({s['identity'] for rows in sessions.values() for s in rows if s['proxy']<30});ids={a:i for i,a in enumerate(identities)};P=len(ids);features={k:[] for k in FEATURES};E=[];R=[];C=[];OBS=[];USER=[];QT=[];REC=[];SID=[];PROXY=[];samples=[];episode_id=0;raw_reentry=0;collisions=0
 for sn,(sid,seq) in enumerate(sessions.items()):
  seenraw=set()
  for x in seq:
   raw_reentry+=x['identity'] in seenraw;seenraw.add(x['identity'])
  pending={}
  for i,x in enumerate(seq):
   q=x['t'];name=x['identity'];proxy=x['proxy']
   if proxy>=30:pending={};continue
   pending.pop(name,None)
   if i and seq[i-1]['proxy']<30:
    prev=seq[i-1]['identity'];pending[prev]=(episode_id,q);episode_id+=1
   if not pending:continue
   until=next((z['t'] for z in seq[i+1:] if z['proxy']>=30),seq[-1]['end']);hist=seq[max(0,i-19):i+1];ha=[z['proxy'] for z in hist];ht=[z['t'] for z in hist];dur=[max(0,hist[k+1]['t']-z['t']) if k+1<len(hist) else 1 for k,z in enumerate(hist)];pad=20-len(hist);opened=np.zeros(32,np.float32);opened[[VOCAB[ident] if kind=='functional' else VOCAB[PROCESS_MAP[ident]] for ident in pending]+[proxy]]=1;dt=datetime.datetime.fromtimestamp(q,datetime.timezone.utc)
   f=dict(history_apps=np.array([30]*pad+ha),history_durations=np.array([0]*pad+dur,np.float32),history_mask=np.array([0]*pad+[1]*len(hist),bool),opened_apps=opened,current_app=proxy,time_feature=np.array([dt.hour/23,dt.weekday()/6,float(dt.weekday()>=5)],np.float32),user_group=0,explicit=explicit_features(ha,ht,q,32))
   for k in FEATURES:features[k].append(f[k])
   ep=np.full(P,-1,np.int32);rem=np.full(P,np.nan);cens=np.ones(P,bool);rec=np.zeros(P);pr=np.full(P,31,np.int64)
   for ident,(eid,leave) in pending.items():
    a=ids[ident];ep[a]=eid;rec[a]=q-leave;pr[a]=VOCAB[ident] if kind=='functional' else VOCAB[PROCESS_MAP[ident]];nxt=next((z['t'] for z in seq[i+1:] if z['identity']==ident and z['t']>q and z['t']<until),None)
    if nxt is not None:rem[a]=nxt-q;cens[a]=False
    samples.append(dict(kind=kind,session=sid,episode=eid,identity=ident,query_time=q,leave=leave,next_entry=nxt,observed_until=until,proxy=int(pr[a])))
   collisions+=len(np.unique(pr[ep>=0]))<sum(ep>=0) or proxy in pr[ep>=0]
   E.append(ep);R.append(rem);C.append(cens);REC.append(rec);PROXY.append(pr);OBS.append(until-q);QT.append(q);USER.append(int(sid.split('/')[0][2:]));SID.append(sn)
 d={k:np.array(v) for k,v in features.items()};d.update(episode=np.array(E),eligible=np.array(E)>=0,remaining=np.array(R),censored=np.array(C),observed_s=np.array(OBS),query_time=np.array(QT),recency=np.array(REC),user=np.array(USER),session=np.array(SID),proxy=np.array(PROXY));observed_ids=np.unique(d['episode'][d['eligible']&~d['censored']]);censored_ids=np.unique(d['episode'][d['eligible']&d['censored']]);stats['observed_proxy_episodes']=len(observed_ids);stats['censored_proxy_episodes']=len(censored_ids);stats['mixed_label_episode_ids']=len(np.intersect1d(observed_ids,censored_ids));assert not stats['mixed_label_episode_ids']
 stats.update(mapped_identities=len(ids),raw_sequence_reentry=raw_reentry,queries=len(E),episodes=episode_id,proxy_collision_queries=collisions,identity_to_proxy={a:VOCAB[a] if kind=='functional' else VOCAB[PROCESS_MAP[a]] for a in ids},unmapped_process_policy='ambiguous ApplicationFrameHost and unsupported utilities are explicit barriers; process labels are never collapsed to proxy identity')
 dest=OUT/'a11y'/kind;dest.mkdir(parents=True,exist_ok=True);write(dest/'audit.json',stats);write(dest/'candidate_rows.json',samples);return d,stats

def calibration(s,d):
 y,m=labels(d['remaining'],d['censored'],d['observed_s'][:,None]);m &= d['eligible'][...,None];ne=int(d['episode'].max())+1;w=np.zeros_like(d['remaining'])
 q,a=np.nonzero(d['eligible']);ep=d['episode'][q,a];count=np.bincount(ep,minlength=ne);w[q,a]=1/count[ep];out={}
 for k,t in enumerate(TH):
  valid=m[...,k];ww=w[valid];pred=s[...,k][valid];truth=y[...,k][valid];out[str(int(t))]={'known_candidate_rows':int(valid.sum()),'episode_weighted_brier':float(np.sum(ww*(pred-truth)**2)/ww.sum()) if ww.sum() else None,'weighted_observed_survival':float(np.sum(ww*truth)/ww.sum()) if ww.sum() else None,'weighted_predicted_survival':float(np.sum(ww*pred)/ww.sum()) if ww.sum() else None}
 return out

def main():
 torch.set_num_threads(1);c=torch.load(BASE/'baseline/M0/checkpoint.pt',map_location='cpu',weights_only=False);m0=AppLSTMVisitExplicit(**c['model_args']);m0.load_state_dict(c['model_state_dict']);m0.eval();c1=torch.load(BASE/'checkpoints/M1.pt',map_location='cpu',weights_only=False);m1=LSTMSegmentedReentry(num_segments=8,**c1['model_args']);m1.load_state_dict(c1['state_dict']);m1.eval();results={}
 for kind in ('functional','process'):
  d,audit=build(kind);f=batch(d,np.arange(len(d['query_time'])));take=lambda p:p[np.arange(len(p))[:,None],d['proxy']];results[kind]={'audit':audit,'models':{}}
  with torch.no_grad():p0=take(visit_probabilities(m0(**f)).numpy());p1=take(m1(**f).softmax(-1).numpy())
  for mode in CFG['sampling_modes']:
   c=torch.load(OUT/mode/'checkpoint.pt',map_location='cpu',weights_only=False);m=OrdinalReentry(thresholds_s=c['thresholds_s'],**c['model_args']);m.load_state_dict(c['state_dict']);m.eval()
   with torch.no_grad():s=take(m(**f).sigmoid().numpy())
   r=risk_coldness(s,TH)
   # External short-PC validation deliberately compares continuous scores, no 8-bin quantization.
   scores=[d['recency'],1-p0[:,:,1].astype(float),p1@np.arange(8),r['coldness']];report,support=metric_rows(d,scores);report['M2 continuous coldness']=report.pop('B3 M2 RankOnly');results[kind]['models'][mode]={'ranking':report,'ordinal_calibration':calibration(s,d)};np.savez_compressed(OUT/'a11y'/kind/f'{mode}-predictions.npz',survival=s,risk30=r['risk30'],risk180=r['risk180'],coldness=r['coldness'],uncertainty=r['uncertainty'],episode=d['episode'],remaining=d['remaining'],censored=d['censored'],query_time=d['query_time'])
 write(OUT/'a11y/validation.json',results);print('A11y validation COMPLETE: functional and process, frozen models',flush=True)
if __name__=='__main__':main()
