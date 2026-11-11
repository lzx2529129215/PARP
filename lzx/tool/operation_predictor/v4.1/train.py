"""Reproducible PC RankProp v4.1 reconstruction; not original v4 training code.
Current-context return-distance prediction + set-conditioned residual ranker.
Labels use only complete within-split 16-switch futures; checkpoint selection
uses validation only. Shared 15-app vocabulary; inference never reads labels.
"""
import os
os.environ.setdefault('OMP_NUM_THREADS','1')
import csv,json,math,time,random,hashlib,argparse,sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset,DataLoader
ROOT=Path(__file__).resolve().parents[4]; OP=ROOT/'lzx/tool/operation_predictor'
DATA=OP/'data/lsapp_expanded/processed/app_lstm_duration_switch'
VOC=json.loads((OP/'data/vocab/lsapp_expanded/app_vocab_duration.json').read_text())
SEED=20261030; H=16; N=15; L=32

def load(split):
 groups=defaultdict(list)
 with (DATA/f'{split}.csv').open() as f:
  for r in csv.DictReader(f):
   if r['trigger_type']=='foreground_transition':groups[r['session_id']].append(r)
 return {k:sorted(v,key=lambda r:r['timestamp']) for k,v in groups.items()}

def encode(rs,i):
 hist=rs[max(0,i-L+1):i+1];ids=[15]*L;num=np.zeros((L,2),np.float32);mask=np.zeros(L,bool)
 for j,r in enumerate(hist):
  t=datetime.fromisoformat(r['timestamp']);idx=L-len(hist)+j;ids[idx]=VOC[r['current_app']];mask[idx]=True
  num[idx,0]=(t.hour*3600+t.minute*60+t.second)/86400
  if j:num[idx,1]=min(1,math.log1p(max(0,(t-datetime.fromisoformat(hist[j-1]['timestamp'])).total_seconds()))/math.log1p(7*86400))
 last={};count=defaultdict(int)
 for j,r in enumerate(rs[:i+1]):last[r['current_app']]=j;count[r['current_app']]+=1
 candidates=sorted((a for a in last if a!=rs[i]['current_app']),key=lambda a:(last[a],a))
 cids=[15]*N;feat=np.zeros((N,4),np.float32);cmask=np.zeros(N,bool)
 for j,a in enumerate(candidates):
  cids[j]=VOC[a];cmask[j]=True
  distinct=len({r['current_app'] for r in rs[last[a]+1:i+1]})
  feat[j]=[1-j/max(1,len(candidates)-1),min(16,distinct)/16,min(32,i-last[a])/32,count[a]/(count[a]+10)]
 return (np.array(ids),num,mask,np.array(cids),feat,cmask),candidates

class Samples(Dataset):
 def __init__(self,groups):
  self.x=[];self.y=[];self.keys=[]
  for sid,rs in sorted(groups.items()):
   if any(r['current_app'] not in VOC or VOC[r['current_app']]>=15 for r in rs):continue
   for i in range(10,len(rs)-H):
    x,cands=encode(rs,i)
    if len(cands)<3:continue
    future=[r['current_app'] for r in rs[i+1:i+H+1]]
    y=np.zeros(N,np.int64)
    for j,a in enumerate(cands):y[j]=future.index(a) if a in future else H
    self.x.append(x);self.y.append(y);self.keys.append((sid,i))
 def __len__(self):return len(self.y)
 def __getitem__(self,i):return tuple(torch.from_numpy(v.copy()) for v in self.x[i]),torch.from_numpy(self.y[i].copy())

class RankProp41(nn.Module):
 def __init__(self):
  super().__init__();self.emb=nn.Embedding(16,16,padding_idx=15);self.proj=nn.Linear(18,32)
  self.register_buffer('pos',torch.randn(1,L,32)*.02)
  layer=nn.TransformerEncoderLayer(32,4,64,.1,batch_first=True)
  self.encoder=nn.TransformerEncoder(layer,1,enable_nested_tensor=False)
  self.dist=nn.Sequential(nn.Linear(32+16+4,64),nn.GELU(),nn.Dropout(.1),nn.Linear(64,17))
  self.ranker=nn.Sequential(nn.Linear(7+7*4+1,32),nn.GELU(),nn.Linear(32,1))
  nn.init.zeros_(self.ranker[-1].weight);nn.init.zeros_(self.ranker[-1].bias)
  self.register_buffer('distances',torch.arange(1,18,dtype=torch.float32))
 def forward(self,ids,num,mask,cids,feat,cmask):
  z=self.proj(torch.cat((self.emb(ids),num),-1))+self.pos
  z=self.encoder(z,src_key_padding_mask=~mask)[:,-1,:]
  context=z.unsqueeze(1).expand(-1,N,-1)
  logits=self.dist(torch.cat((context,self.emb(cids),feat),-1))
  probs=logits.softmax(-1);expected=(probs*self.distances).sum(-1)/17
  v=torch.cat((feat,expected.unsqueeze(-1),probs[:,:,-1:],probs[:,:,:3].sum(-1,keepdim=True)),-1)
  m=cmask.unsqueeze(-1);count=m.sum(1).clamp(min=1)
  mean=(v*m).sum(1)/count
  std=(((v-mean.unsqueeze(1))**2*m).sum(1)/count+1e-8).sqrt()
  vmin=v.masked_fill(~m,1e6).amin(1);vmax=v.masked_fill(~m,-1e6).amax(1)
  pool=torch.cat((mean,std,vmin,vmax,count/15),-1).unsqueeze(1).expand(-1,N,-1)
  delta=.15*self.ranker(torch.cat((v,pool),-1)).squeeze(-1).tanh()
  return logits,expected+delta,expected

def loss_fn(logits,score,base,y,mask):
 ce=nn.functional.cross_entropy(logits[mask],y[mask])
 diff=y.unsqueeze(2)-y.unsqueeze(1);valid=mask.unsqueeze(2)&mask.unsqueeze(1)&(diff>0)
 sd=score.unsqueeze(2)-score.unsqueeze(1)
 weight=diff.float().clamp(min=0)/16
 pair=(nn.functional.softplus(-sd*8)*weight)[valid].mean() if valid.any() else score.sum()*0
 return ce+.7*pair+.05*((score-base)**2)[mask].mean()

@torch.inference_mode()
def evaluate(model,loader):
 model.eval();tot=0;hit=0;early=0;loss=0.;reg=0.
 for x,y in loader:
  logits,s,b=model(*x);mask=x[-1];chosen=s.masked_fill(~mask,-1e6).argmax(1);chosen_y=y.gather(1,chosen[:,None]).squeeze(1)
  best=y.masked_fill(~mask,-1).max(1).values
  hit+=int((chosen_y==best).sum());early+=int((chosen_y<3).sum());reg+=float((best-chosen_y).sum());tot+=len(y)
  loss+=float(loss_fn(logits,s,b,y,mask))*len(y)
 return dict(n=tot,optimal=hit/tot,early3=early/tot,regret=reg/tot,loss=loss/tot)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--epochs',type=int,default=12);args=ap.parse_args()
 torch.set_num_threads(1);random.seed(SEED);np.random.seed(SEED);torch.manual_seed(SEED)
 here=Path(__file__).resolve().parent;out=here/'outputs';out.mkdir(exist_ok=True)
 t=time.time();train=Samples(load('train'));val=Samples(load('val'))
 # Labels and feature arrays remain entirely inside their source split.
 print(json.dumps(dict(train=len(train),val=len(val),prepare_seconds=time.time()-t)),flush=True)
 assert train and val
 trainkeys={s for s,i in train.keys};valkeys={s for s,i in val.keys}
 dl=DataLoader(train,batch_size=128,shuffle=True);vl=DataLoader(val,batch_size=256)
 model=RankProp41();optimizer=torch.optim.AdamW(model.parameters(),lr=1e-3,weight_decay=.01)
 best=-1e9;history=[]
 for epoch in range(args.epochs):
  model.train();running=0.;seen=0
  for x,y in dl:
   logits,s,b=model(*x);loss=loss_fn(logits,s,b,y,x[-1]);optimizer.zero_grad();loss.backward();nn.utils.clip_grad_norm_(model.parameters(),1.);optimizer.step();running+=float(loss)*len(y);seen+=len(y)
  metrics=evaluate(model,vl);objective=metrics['optimal']-.1*metrics['early3'];row=dict(epoch=epoch+1,train_loss=running/seen,val=metrics,seconds=time.time()-t);history.append(row);print(json.dumps(row),flush=True)
  if objective>best:
   best=objective;torch.save(dict(state=model.state_dict(),epoch=epoch+1,validation=metrics),out/'best.pt')
 ckpt=torch.load(out/'best.pt',weights_only=False);model.load_state_dict(ckpt['state']);model.eval()
 example=tuple(v.unsqueeze(0) for v in val[0][0]);traced=torch.jit.trace(model,example,check_trace=False)
 traced.save(str(here/'model/rankprop_v4_1.ts'))
 # Reload and verify predictions on multiple candidate counts/batches.
 loaded=torch.jit.load(str(here/'model/rankprop_v4_1.ts'));maxerr=0.
 with torch.inference_mode():
  for x,y in vl:
   maxerr=max(maxerr,float((loaded(*x)[1]-model(*x)[1]).abs().max()))
 assert maxerr<1e-5
 # Test remains untouched until checkpoint selection has finished.
 test=Samples(load('test'));tl=DataLoader(test,batch_size=256);tm=evaluate(model,tl)
 config=dict(version='v4.1',seed=SEED,horizon=H,context=L,num_apps=N,vocab=VOC,training_samples=len(train),validation_samples=len(val),test_samples=len(test),best_epoch=ckpt['epoch'],validation=ckpt['validation'],test=tm,export_max_error=maxerr,history=history,architecture='Compact Transformer distance distribution + candidate-set residual ranker; reconstructed training, not original v4 source',distance_definition='Number of future APP_SWITCH events until candidate returns; class 17 = not returned in complete 16-switch window',candidate_policy='Apps observed so far in current session remain resident; exclude current foreground',selection='max validation optimal minus 0.1*early3; no test selection',split_session_overlap=len(trainkeys&valkeys),files={str(DATA/f'{s}.csv'):hashlib.sha256((DATA/f'{s}.csv').read_bytes()).hexdigest() for s in ['train','val','test']})
 (out/'training.json').write_text(json.dumps(config,indent=2));print('FINAL',json.dumps(tm),flush=True)
 # Score exactly the previous comparison anchors, including 8/32-window subsets.
 previous=ROOT/'test/outputs/rankprop_v3_compare_20261030/decisions.csv';groups=load('test');decisions=[]
 with previous.open() as f: prior=list(csv.DictReader(f))
 cache={}
 with torch.inference_mode():
  for r in prior:
   if r['method']!='v3':continue
   sid=r['session'];i=int(r['index']);h=int(r['horizon']);key=(sid,i)
   if key not in cache:
    x,cands=encode(groups[sid],i);xt=tuple(torch.from_numpy(v.copy()).unsqueeze(0) for v in x)
    _,scores,_=model(*xt);order=sorted(range(len(cands)),key=lambda j:(-float(scores[0,j]),j));cache[key]=[cands[j] for j in order]
   order=cache[key];future=[z['current_app'] for z in groups[sid][i+1:i+h+1]];d={a:future.index(a)+1 if a in future else h+1 for a in order};best=max(d.values());pairs=[(a,b) for j,a in enumerate(order) for b in order[j+1:] if d[a]!=d[b]]
   decisions.append(dict(session=sid,index=i,horizon=h,method='v4.1',victim=order[0],optimal=int(d[order[0]]==best),early3=int(d[order[0]]<=3),regret=best-d[order[0]],pair_ok=sum(d[a]>d[b] for a,b in pairs),pairs=len(pairs)))
 with (out/'decisions.csv').open('w') as f:
  w=csv.DictWriter(f,fieldnames=list(decisions[0]));w.writeheader();w.writerows(decisions)
 summary=[]
 for h in [8,16,32]:
  rr=[r for r in decisions if r['horizon']==h];summary.append(dict(horizon=h,n=len(rr),optimal=np.mean([r['optimal'] for r in rr]),early3=np.mean([r['early3'] for r in rr]),regret=np.mean([r['regret'] for r in rr]),pair_accuracy=sum(r['pair_ok'] for r in rr)/sum(r['pairs'] for r in rr)))
 (out/'comparison.json').write_text(json.dumps(summary,indent=2));print('COMPARISON',json.dumps(summary),flush=True)
if __name__=='__main__':main()
