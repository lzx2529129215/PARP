#!/usr/bin/env python3
import argparse,hashlib,json,sys,time
from pathlib import Path
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from models.segmented import LSTMSegmentedReentry,masked_ce,MODEL_TYPE
from models.app_lstm_visit_explicit import FEATURE_NAMES
from dataset.build import write

FEATURES=['history_apps','history_durations','history_mask','opened_apps','current_app','time_feature','user_group','explicit']


def load_data(path):return {p.stem:np.load(p,mmap_mode='r') for p in path.glob('*.npy')}
def batch(data,indices):return {k:torch.from_numpy(np.array(data[k][indices],copy=True)) for k in FEATURES}


@torch.no_grad()
def evaluate(model,data,indices,bs):
    model.eval();total=count=0
    for start in range(0,len(indices),bs):
        ids=indices[start:start+bs];mask=torch.from_numpy(np.array(data['label_valid'][ids],copy=True));y=torch.from_numpy(np.array(data['labels'][ids],copy=True))
        logits=model(**batch(data,ids));n=int(mask.sum());total+=float(masked_ce(logits,y,mask))*n;count+=n
    return total/count if count else None


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();cfg=json.loads(a.config.read_text());out=Path(cfg['output'])
    assert json.loads((out/'dataset/validation.json').read_text())['status']=='PASS'
    torch.set_num_threads(cfg['threads']);torch.manual_seed(cfg['seed']);np.random.seed(cfg['seed'])
    meta=json.loads((out/'baseline/dataset/dataset_meta.json').read_text());args=dict(num_apps=len(meta['app_vocab']),num_user_groups=len(meta['group_vocab']),pad_id=meta['app_vocab']['<PAD>'],duration_cap_s=cfg['duration_cap_s'])
    model=LSTMSegmentedReentry(num_segments=len(cfg['reentry_bins'])+1,**args)
    train=load_data(out/'dataset/train');val=load_data(out/'dataset/val')
    ids={s:np.flatnonzero(d['label_valid'].any(1)) for s,d in [('train',train),('val',val)]}
    # Empty/all-unknown anchors have zero supervised contribution. Drop them
    # before batching, preserving every valid candidate and every valid C7.
    assert all(len(x)>0 for x in ids.values())
    settings={**cfg,'model_args':args,'supervised_query_counts':{s:len(x) for s,x in ids.items()},'train_valid_candidates':int(train['label_valid'].sum()),
        'source_hashes':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for folder in ['scripts','models','dataset'] for f in (ROOT/folder).glob('*.py')}}
    write(out/'metrics/training-config.json',settings);print('training settings',json.dumps(settings),flush=True)
    optimizer=torch.optim.Adam(model.parameters(),lr=cfg['lr']);torch.manual_seed(cfg['seed']);rng=torch.Generator().manual_seed(cfg['seed'])
    history=[];best=float('inf');started=time.monotonic()
    for epoch in range(1,cfg['epochs']+1):
        model.train();order=ids['train'][torch.randperm(len(ids['train']),generator=rng).numpy()];total=count=0
        write(out/'progress.json',{'phase':'training','epoch':epoch,'epochs':cfg['epochs'],'supervised_queries':len(order)})
        for start in range(0,len(order),cfg['batch_size']):
            ix=order[start:start+cfg['batch_size']];mask=torch.from_numpy(np.array(train['label_valid'][ix],copy=True));y=torch.from_numpy(np.array(train['labels'][ix],copy=True))
            logits=model(**batch(train,ix));loss=masked_ce(logits,y,mask)
            if not torch.isfinite(loss):raise RuntimeError('nonfinite loss')
            optimizer.zero_grad(set_to_none=True);loss.backward();optimizer.step();n=int(mask.sum());total+=float(loss)*n;count+=n
        vl=evaluate(model,val,ids['val'],cfg['batch_size']);row={'epoch':epoch,'train_ce':total/count,'val_ce':vl,'elapsed_s':time.monotonic()-started};history.append(row)
        write(out/'metrics/training-history.json',history);print(json.dumps(row),flush=True)
        if vl<best:
            best=vl
            torch.save({'model_type':MODEL_TYPE,'model_args':args,'num_segments':len(cfg['reentry_bins'])+1,'reentry_bins':cfg['reentry_bins'],
                'history_len':20,'feature_names':FEATURE_NAMES,'app_vocab':meta['app_vocab'],'group_vocab':meta['group_vocab'],
                'state_dict':model.state_dict(),'epoch':epoch,'val_ce':vl,'config':settings,'score_definition':'sum(k*p_k); larger is colder',
                'label_definition':'strictly future next foreground entry minus query time; bins left-closed/right-open; unknown censored masked'},out/'checkpoints/M1.pt')
    (out/'TRAINING-REPORT.md').write_text('# Training\n\nPhase 4: PASS\n\nM1 = LSTM-Segmented-Reentry。只替换 LSTM 与显式分支的末层 2→8 输出，encoder 初始参数与 M0 同seed一致；从头训练，保留原Adam/lr/batch/epochs。\n\n'+json.dumps({'best_val_ce':best,'epochs':history,'settings':settings},ensure_ascii=False,indent=2)+'\n\n零候选/全部删失未知query保存在数据里，但无CE监督贡献，不执行无效梯度步骤。有效样本均保留；没有class weighting、ranking或survival loss。\n')
    write(out/'progress.json',{'phase':'training','status':'PASS','next':'offline evaluation'})


if __name__=='__main__':main()
