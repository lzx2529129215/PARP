from common import *
from model import OrdinalReentry,episode_bce
import torch,time,argparse

def selected_weights(data,mode,rng):
 w=np.asarray(data['weights']);ep=np.asarray(data['episode']);valid=w>0
 if mode=='episode_weighted':return w
 flat=np.flatnonzero(valid);eid=ep.ravel()[flat];order=np.lexsort((rng.random(len(flat)),eid));chosen=order[np.r_[True,np.diff(eid[order])!=0]]
 out=np.zeros(w.shape,np.float32);out.ravel()[flat[chosen]]=1.;assert len(chosen)==len(np.unique(eid));return out

def main(mode):
 limits=CFG.get('sampling_training_limits',{}).get(mode,CFG['early_stopping']);torch.set_num_threads(CFG['threads']);torch.manual_seed(CFG['seed']);rng=np.random.default_rng(CFG['seed']);train=compact('train');val=compact('val');args=dict(num_apps=len(VOCAB),num_user_groups=1,pad_id=VOCAB['<PAD>'],duration_cap_s=600)
 model=OrdinalReentry(**args);optim=torch.optim.Adam(model.parameters(),lr=CFG['lr']);bs=CFG['batch_size'];best=float('inf');best_epoch=0;history=[];start=time.time();dest=OUT/mode;dest.mkdir(exist_ok=True);vi=np.flatnonzero(val['weights'].sum(1)>0)
 episode_n=int(np.sum(np.asarray(train['weights']))+.5);val_n=float(val['weights'].sum());print(mode,'episodes',episode_n,'train queries',len(train['weights']),'val queries',len(vi),flush=True)
 for epoch in range(1,limits['max_epochs']+1):
  model.train();w=selected_weights(train,mode,rng);ids=np.flatnonzero(w.sum(1)>0);rng.shuffle(ids);steps=(len(ids)+bs-1)//bs;norm=episode_n/steps;loss_total=0
  for begin in range(0,len(ids),bs):
   ix=ids[begin:begin+bs];logits=model(**batch(train,ix));raw=episode_bce(logits,torch.from_numpy(np.array(train['y'][ix])),torch.from_numpy(np.array(train['mask'][ix])),torch.from_numpy(np.array(w[ix])));loss=raw/norm
   if not torch.isfinite(loss):raise RuntimeError('nonfinite loss')
   optim.zero_grad(set_to_none=True);loss.backward();optim.step();loss_total+=float(raw.detach())
  model.eval();vl=0
  with torch.no_grad():
   for begin in range(0,len(vi),bs):
    ix=vi[begin:begin+bs];vl+=float(episode_bce(model(**batch(val,ix)),torch.from_numpy(np.array(val['y'][ix])),torch.from_numpy(np.array(val['mask'][ix])),torch.from_numpy(np.array(val['weights'][ix]))))
  vl/=val_n;row=dict(epoch=epoch,train_episode_bce=loss_total/episode_n,val_episode_bce=vl,queries=len(ids),elapsed_s=time.time()-start);history.append(row);write(dest/'history.json',history);print(mode,json.dumps(row),flush=True)
  if vl<best:
   best=vl;best_epoch=epoch;torch.save(dict(model_type='ordinal_survival_v1',model_args=args,thresholds_s=CFG['thresholds_s'],state_dict=model.state_dict(),epoch=epoch,val_episode_bce=vl,config=CFG,sampling=mode),dest/'checkpoint.pt')
  if epoch>=limits['min_epochs'] and epoch-best_epoch>=limits['patience']:break
 write(dest/'status.json',dict(status='COMPLETE',best_val_bce=best,epochs=len(history)))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('mode',choices=CFG['sampling_modes']);main(p.parse_args().mode)
