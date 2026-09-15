import argparse,time
from common import *
import torch
from models.segmented import LSTMSegmentedReentry,masked_ce,MODEL_TYPE
from models.app_lstm_visit_explicit import FEATURE_NAMES
from scripts.train import evaluate

def main():
    p=argparse.ArgumentParser();p.add_argument('variant',choices=['switch','original']);args=p.parse_args();variant=args.variant
    meta=META if variant=='switch' else json.loads(Path(CFG['raw_meta']).read_text());dataset=BASE/'dataset' if variant=='switch' else OUT/'dataset/original'
    if variant=='original':assert json.loads((dataset/'validation.json').read_text())['status']=='PASS'
    torch.set_num_threads(CFG['threads']);torch.manual_seed(42);np.random.seed(42)
    ma=dict(num_apps=len(meta['app_vocab']),num_user_groups=1,pad_id=meta['app_vocab']['<PAD>'],duration_cap_s=600)
    model=LSTMSegmentedReentry(num_segments=8,**ma);train=load_data(dataset/'train');val=load_data(dataset/'val')
    ti=np.flatnonzero(train['label_valid'].any(1)&((train['trigger']==0) if variant=='switch' else True));vi=np.flatnonzero(val['label_valid'].any(1));assert len(ti) and len(vi)
    # Compact the exact selected rows in sorted original order. This changes
    # storage access only: the same seeded permutation addresses identical
    # feature/label tensors in the same minibatch order.
    from scripts.train import FEATURES
    def compact(data,indices):
        selected={}
        for key in FEATURES+['labels','label_valid']:
            selected[key]=np.asarray(data[key][indices])
        return selected
    train=compact(train,ti);val=compact(val,vi)
    ti=np.arange(len(ti));vi=np.arange(len(vi))
    conf={**CFG,'variant':variant,'model_args':ma,'train_queries':len(ti),'validation_queries':len(vi),'parameters':sum(p.numel() for p in model.parameters()),'source_hashes':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for folder in [ROOT/'root_cause',ROOT/'models'] for f in folder.glob('*.py')}}
    write(OUT/f'metrics/{variant}-training-config.json',conf)
    optimizer=torch.optim.Adam(model.parameters(),lr=CFG['lr']);torch.manual_seed(42);rng=torch.Generator().manual_seed(42);best=float('inf');history=[];start=time.monotonic()
    for epoch in range(1,21):
        model.train();order=ti[torch.randperm(len(ti),generator=rng).numpy()];total=count=0
        for begin in range(0,len(order),2048):
            ix=order[begin:begin+2048];m=torch.from_numpy(np.array(train['label_valid'][ix]));y=torch.from_numpy(np.array(train['labels'][ix]));loss=masked_ce(model(**batch(train,ix)),y,m)
            assert torch.isfinite(loss);optimizer.zero_grad(set_to_none=True);loss.backward();optimizer.step();n=int(m.sum());count+=n;total+=float(loss)*n
        vl=evaluate(model,val,vi,2048);row=dict(epoch=epoch,train_ce=total/count,val_ce=vl,elapsed_s=time.monotonic()-start);history.append(row);print(variant,json.dumps(row),flush=True)
        write(OUT/f'metrics/{variant}-training-history.json',history);write(OUT/f'metrics/{variant}-progress.json',{'epoch':epoch,'epochs':20,'status':'RUNNING'})
        if vl<best:
            best=vl;torch.save(dict(model_type=MODEL_TYPE,model_args=ma,num_segments=8,reentry_bins=CFG['reentry_bins'],history_len=20,feature_names=FEATURE_NAMES,app_vocab=meta['app_vocab'],group_vocab=meta['group_vocab'],state_dict=model.state_dict(),epoch=epoch,val_ce=vl,config=conf),OUT/f'checkpoints/M1-{variant}.pt')
    write(OUT/f'metrics/{variant}-progress.json',{'epoch':20,'status':'PASS','best_val_ce':best})
if __name__=='__main__':main()
