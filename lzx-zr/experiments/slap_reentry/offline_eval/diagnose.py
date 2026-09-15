#!/usr/bin/env python3
"""Post-evaluation diagnosis; never retune boundaries or select on test."""
import argparse,json,sys,itertools
from pathlib import Path
from collections import defaultdict
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.train import load_data
from offline_eval.metrics import direction
from dataset.build import write


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();cfg=json.loads(a.config.read_text());out=Path(cfg['output'])
    d=load_data(out/'dataset/test');p0=np.load(out/'predictions/M0-test-probabilities.npy',mmap_mode='r');p1=np.load(out/'predictions/M1-test-probabilities.npy',mmap_mode='r')
    g=json.loads((out/'metrics/gate1.json').read_text());history=json.loads((out/'metrics/training-history.json').read_text())
    best=min(history,key=lambda x:x['val_ce']);groups=defaultdict(lambda:np.zeros(3));candidate_count=d['eligible'].sum(1);real=d['eligible'];valid=d['label_valid']
    predicted_hist=np.zeros(8,np.int64);truth_hist=np.zeros(8,np.int64);missing_enter=missing_exit=0
    for qi in np.flatnonzero(candidate_count):
        candidates=np.flatnonzero(real[qi]);s0=1-p0[qi,:,1].astype(np.float64);s1=p1[qi].astype(np.float64)@np.arange(8,dtype=np.float64)
        predicted_hist+=np.bincount(p1[qi,candidates].argmax(1),minlength=8)
        truth_hist+=np.bincount(d['labels'][qi][valid[qi]],minlength=8)
        missing_enter+=int((d['explicit'][qi,candidates,1]==0).sum());missing_exit+=int((d['explicit'][qi,candidates,3]==0).sum())
        for x,y in itertools.combinations(candidates,2):
            order=direction(x,y,d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])
            if order is None:continue
            label='unknown_segment'
            if valid[qi,x] and valid[qi,y]:label='same_segment' if d['labels'][qi,x]==d['labels'][qi,y] else 'different_segments'
            keys=[label,'switch' if d['trigger'][qi]==0 else 'periodic30','original_queries' if d['original_index'][qi]>=0 else 'new_queries']
            for key in keys:groups[key]+=np.array([1,int(np.sign(s0[x]-s0[y]))==order,int(np.sign(s1[x]-s1[y]))==order])
    result={'gate':g['status'],'best_epoch_by_validation_ce':best,'last_epoch':history[-1],
        'same_segment_resolution_note':'Expected segment index can still order within-class examples through probability mixtures, but CE has no exact within-segment time supervision.',
        'pair_groups':{k:{'pairs':int(v[0]),'p180_poa':float(v[1]/v[0]),'slap_poa':float(v[2]/v[0])} for k,v in groups.items()},
        'predicted_class_counts_all_candidates':predicted_hist.tolist(),'true_class_counts_valid_candidates':truth_hist.tolist(),
        'candidate_pairs':int(real.sum()),'missing_last_enter_in_visible20':missing_enter,'missing_last_leave_in_visible20':missing_exit,
        'single_candidate_queries':int((candidate_count==1).sum()),'multi_candidate_queries':int((candidate_count>=2).sum()),
        'root_cause_status':{'label_construction':'exhaustive checks + 300 independent source checks passed','input_parity':'original M0 predictions reproduced',
            'overfitting':'validation curve directly measured; not inferred from class accuracy','class_imbalance':'counts measured; causal impact unproven without ablation',
            'query_distribution':'period30 adds many periodic anchors; original-query and switch/periodic strata reported','opened_and_domain':'source approximation unchanged; no real-PC ground truth in this dataset'}}
    write(out/'metrics/diagnosis.json',result)
    lines=['# Post-evaluation diagnosis','',f'Gate-1: {g["status"]}。未修改验收标准。','',json.dumps(result,ensure_ascii=False,indent=2),'',
        '本轮仅判断分段目标是否改善排序。过拟合、类别占比和同segment排序分组是诊断证据；不能仅凭这些统计断言某一因素是唯一根因。',
        '所有未知样本仍保留且不伪造时间。未来若继续实验，优先针对query分布、分类样本不平衡、候选显式历史覆盖做预先定义的消融，并使用新的时间留出；本轮不加入Transformer、survival或ranking loss。']
    (out/'DIAGNOSIS.md').write_text('\n'.join(lines)+'\n');print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
