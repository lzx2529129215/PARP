#!/usr/bin/env python3
import argparse,bisect,csv,json,sys,time
from collections import defaultdict
from pathlib import Path
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from models.segmented import LSTMSegmentedReentry,predict
from models.app_lstm_visit_explicit import AppLSTMVisitExplicit
from models.app_lstm_visit_window import visit_probabilities
from scripts.train import load_data,batch
from dataset.build import write,stamp,iso
from offline_eval.metrics import query_metrics,stats,bootstrap_ratio,bootstrap_median_difference

NAMES=['Recency','p180','SLAP-style']
KEYS=['poa_numerator','poa_denominator','score_tied_pairs','victim','victim_time','victim_censored','dvr30','dvr180','cp1','cp2','cp3','evr2','evr4']


def summarize(values,selection):
    output={}
    for mi,name in enumerate(NAMES):
        a=values[selection,mi,:];col=lambda k:a[:,KEYS.index(k)]
        num=np.nansum(col('poa_numerator'));den=np.nansum(col('poa_denominator'))
        r={'queries':int(selection.sum()),'poa':float(num/den) if den else None,'comparable_pairs':int(den),'correct_pairs':int(num),'score_tied_pairs':int(np.nansum(col('score_tied_pairs')))}
        for key in ['cp1','cp2','cp3','dvr30','dvr180','victim_time','evr2','evr4']:r[key]=stats(col(key))
        r['victim_censored_fraction']=float(np.nanmean(col('victim_censored')))
        output[name]=r
    return output


def gate(values,selected,user,cfg):
    v=values[selected];user=user[selected];checks={}
    den=v[:,1:,KEYS.index('poa_denominator')];num=v[:,1:,KEYS.index('poa_numerator')]
    rates=num.sum(0)/den.sum(0);rel=float(rates[1]/rates[0]-1)
    ci=bootstrap_ratio(num,den,user,cfg['gate_bootstrap_replicates'],cfg['gate_bootstrap_seed'])
    checks['poa']={'p180':float(rates[0]),'slap':float(rates[1]),'relative_improvement':rel,**ci,
        'pass':bool(rel>=cfg['gate_relative_poa_gain'] and ci['ci95'][0] is not None and ci['ci95'][0]>0)}
    improvements=0
    for key in ['cp3','dvr30','dvr180','victim_time','evr2','evr4']:
        # Both methods must have known outcomes on the very same query.
        a=v[:,1:,KEYS.index(key)];valid=np.isfinite(a).all(1);a=a[valid];u=user[valid]
        if not len(a):checks[key]={'n':0,'pass':False};continue
        if key in ['victim_time','evr2','evr4']:
            point=np.median(a,axis=0);ci=bootstrap_median_difference(a,u,cfg['gate_bootstrap_replicates'],cfg['gate_bootstrap_seed'])
        else:
            point=a.mean(0);ci=bootstrap_ratio(a,np.ones_like(a),u,cfg['gate_bootstrap_replicates'],cfg['gate_bootstrap_seed'])
        lower,upper=ci['ci95'];higher=key not in ['dvr30','dvr180']
        passed=bool((point[1]>point[0] and lower>0) if higher else (point[1]<point[0] and upper<0))
        checks[key]={'n':len(a),'p180':float(point[0]),'slap':float(point[1]),**ci,'pass':passed};improvements+=passed
    return {'status':'PASS' if checks['poa']['pass'] and improvements>=cfg['gate_other_improvements_required'] else 'FAIL',
        'definition':'multi-candidate queries; POA >=5% relative gain with paired user-bootstrap CI above zero; >=2 other clear improvements on common known outcomes. Thresholds fixed before M1 training.',
        'other_improvements':improvements,'checks':checks}


def finish_report(out,cfg,data,values,reports,classification,g,confusion):
    counts=data['eligible'].sum(1)
    import matplotlib
    matplotlib.use('Agg');import matplotlib.pyplot as plt
    for scope,sel in [('all',counts>0),('multi',counts>=2)]:
        fig,ax=plt.subplots()
        for mi,name in enumerate(NAMES):
            x=values[sel,mi,KEYS.index('victim_time')];x=np.sort(x[np.isfinite(x)]);ax.step(x,np.arange(1,len(x)+1)/len(x),label=f'{name} observed n={len(x)}')
        ax.set(xlabel='Victim actual remaining reentry (seconds; observed only)',ylabel='Conditional empirical CDF',xscale='symlog');ax.legend();fig.tight_layout();fig.savefig(out/f'figures/victim_reentry_cdf_{scope}.png');plt.close(fig)
    fig,ax=plt.subplots();im=ax.imshow(confusion);ax.set(xlabel='Predicted segment',ylabel='True segment');fig.colorbar(im,ax=ax);fig.tight_layout();fig.savefig(out/'figures/confusion_matrix.png');plt.close(fig)
    distribution=json.loads((out/'dataset/validation.json').read_text())['splits'];write(out/'metrics/segment_distribution.json',distribution)
    fig,ax=plt.subplots()
    for i,(s,d) in enumerate(distribution.items()):
        y=np.array([d['class_distribution_including_unknown'].get(str(k),0) for k in range(8)]);ax.bar(np.arange(8)+(i-1)*.25,y/y.sum(),width=.25,label=s)
    ax.set(xlabel='Segment',ylabel='Fraction among valid classification labels');ax.legend();fig.tight_layout();fig.savefig(out/'figures/segment_distribution.png');plt.close(fig)
    def fmt(v):return 'N/A' if v is None else f'{v:.4f}'
    def table(report):
        rows=['| Metric | Recency | p180 | SLAP-style |','|---|---:|---:|---:|']
        for label,key,field in [('Pairwise ordering','poa',None),('Cold Precision@1','cp1','mean'),('Cold Precision@2','cp2','mean'),('Cold Precision@3','cp3','mean'),('DVR@30 ↓','dvr30','mean'),('DVR@180 ↓','dvr180','mean'),('Victim median reentry ↑','victim_time','median'),('EVR@2 median ↑','evr2','median'),('EVR@4 median ↑','evr4','median')]:
            cells=[]
            for method in NAMES:
                r=report[method];value=r[key] if field is None else r[key][field];n=r['comparable_pairs'] if field is None else r[key]['n'];cells.append(f'{fmt(value)} (n={n})')
            rows.append('| '+label+' | '+' | '.join(cells)+' |')
        return rows
    lines=['# Offline evaluation','',f'Phase 5: PASS（评估程序完成）；Gate-1: **{g["status"]}**。', '',
        '三方法完全相同query/candidate。主表使用至少2个后台候选的query。Cold Precision@K使用真实Top-K集合可唯一确定、候选数>K、真实第K/K+1无并列（可利用删失下界；删失候选多于K则不可确定）的query，排除选全候选的平凡高分。',
        'POA允许通过删失下界证明顺序，两个未知次序的删失样本不比较；真实时间并列排除，模型分数并列按未正确排序计。',
        'DVR仅使用能判定 <30/<180 的结果，不把未知当安全。Victim统计/CDF只描述已观察返回的条件分布，未返回不是无穷大，选择性删失可能造成偏差；JSON附每方法删失率。',
        'EVR要求实际至少K候选；仅在最早返回可确定时计入。B0用同会话全部因果历史的最后离开时间，不截到20段；缺失置0且记录。',
        '新增30秒采样改变query分布；保持旧时间边界，因此新增样本比例不一定70/15/15。M1训练样本量也增加，不能将全部提升唯一归因于head。原M0查询子集另列；原M0概率已复现。','']
    lines+=table(reports['multi_candidate'])+['','## 原M0查询、多候选子集','']+table(reports['original_M0_multi'])+['','## 所有非空候选查询（含单候选）','']+table(reports['all_nonempty'])
    lines+=['','## Gate-1（训练前固定）','POA相对提升至少5%，且按用户成组的配对bootstrap 95%CI>0；其他指标至少两项明确改善。其他指标只在两方法均有已知结果的相同query比较，避免删失分母差异。时间指标还要求全局中位数方向正确、配对用户重采样的中位数差CI>0。',json.dumps(g,ensure_ascii=False,indent=2),'','辅助分类指标：'+json.dumps(classification,ensure_ascii=False), '','单seed、重复使用既有LSApp测试时间段，不构成独立新数据泛化证明。']
    (out/'OFFLINE-EVALUATION.md').write_text('\n'.join(lines)+'\n')
    if g['status']=='FAIL':
        for file,title in [('RUNTIME-VALIDATION.md','Runtime'),('KERNEL-EVALUATION.md','Kernel')]:
            (out/file).write_text(f'# {title}\n\nNOT RUN — Gate-1 FAIL，遵守用户门禁，不接入预测、不写内核、不改变回收行为。\n')
    write(out/'progress.json',{'phase':'offline evaluation','status':'PASS','gate1':g['status'],'next':'runtime' if g['status']=='PASS' else 'diagnose failure'})
    print(json.dumps({'gate1':g,'multi_candidate':reports['multi_candidate']},ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--reports-only',action='store_true');args=p.parse_args();cfg=json.loads(args.config.read_text());out=Path(cfg['output']);torch.set_num_threads(cfg['threads'])
    assert (out/'TRAINING-REPORT.md').exists();data=load_data(out/'dataset/test');n=len(data['query_time']);A=data['eligible'].shape[1]
    if args.reports_only:
        result=json.loads((out/'metrics/offline.json').read_text());values=np.load(out/'metrics/query-metrics.npy',mmap_mode='r');confusion=np.loadtxt(out/'metrics/confusion_matrix.csv',delimiter=',',dtype=np.int64)
        finish_report(out,cfg,data,values,result['scopes'],result['classification'],result['gate1'],confusion)
        return
    c0=torch.load(out/'baseline/M0/checkpoint.pt',map_location='cpu',weights_only=False);m0=AppLSTMVisitExplicit(**c0['model_args']);m0.load_state_dict(c0['model_state_dict']);m0.eval()
    c1=torch.load(out/'checkpoints/M1.pt',map_location='cpu',weights_only=False);m1=LSTMSegmentedReentry(num_segments=c1['num_segments'],**c1['model_args']);m1.load_state_dict(c1['state_dict']);m1.eval()
    assert c0['app_vocab']==c1['app_vocab'];assert c1['reentry_bins']==cfg['reentry_bins']
    m0p=np.lib.format.open_memmap(out/'predictions/M0-test-probabilities.npy',mode='w+',dtype='float32',shape=(n,A,2))
    m1p=np.lib.format.open_memmap(out/'predictions/M1-test-probabilities.npy',mode='w+',dtype='float32',shape=(n,A,8))
    lat=[]
    for start in range(0,n,cfg['batch_size']):
        ix=np.arange(start,min(n,start+cfg['batch_size']));f=batch(data,ix)
        with torch.no_grad():
            m0p[ix]=visit_probabilities(m0(**f)).numpy();begin=time.perf_counter();p1,_,_=predict(m1(**f));lat.append((time.perf_counter()-begin,len(ix)));m1p[ix]=p1.numpy()
        if start%(cfg['batch_size']*25)==0:write(out/'progress.json',{'phase':'offline inference','completed':start,'queries':n})
    m0p.flush();m1p.flush()
    assert np.isfinite(m1p).all() and np.allclose(m1p.sum(-1),1,atol=1e-6)
    oi=data['original_index'];where=np.flatnonzero(oi>=0)
    with np.load(out/'baseline/M0/test_predictions.npz') as old:
        difference=float(np.max(np.abs(m0p[where]-old['probabilities'][oi[where]])))
        np.testing.assert_allclose(m0p[where],old['probabilities'][oi[where]],atol=2e-6,rtol=2e-5)
        assert np.array_equal(data['eligible'][where],old['eligible'][oi[where]])
    write(out/'metrics/M0-reproduction.json',{'original_test_queries':len(where),'max_abs_probability_difference':difference,'status':'PASS'})
    meta=json.loads((out/'dataset/meta.json').read_text());sids=meta['session_ids'];vocab=c1['app_vocab'];inv={v:k for k,v in vocab.items()}
    sessions=defaultdict(list)
    with (out/'baseline/dataset/segments.csv').open() as f:
        for row in csv.DictReader(f):sessions[row['session_id']].append((stamp(row['start_time']),vocab[row['app']]))
    past={}
    for sid,items in sessions.items():
        items.sort(key=lambda x:x[0]);perapp=defaultdict(list)
        for i,(t,a) in enumerate(items):perapp[a].append((t,items[i+1][0] if i+1<len(items) else t))
        past[sid]={a:([x[0] for x in v],[x[1] for x in v]) for a,v in perapp.items()}
    values=np.lib.format.open_memmap(out/'metrics/query-metrics.npy',mode='w+',dtype='float64',shape=(n,3,len(KEYS)));values[:]=np.nan
    counts=data['eligible'].sum(1);confusion=np.zeros((8,8),dtype=np.int64);missing_recency=0
    pf=(out/'predictions/predictions.csv').open('w');pw=csv.writer(pf);pw.writerow(['query_index','query_time','candidate_app','p0','p1','p2','p3','p4','p5','p6','p7','predicted_class','coldness_score','recency_score','recency_known','p180_coldness','actual_class','remaining_sec','is_censored','label_valid','original_index'])
    rf=(out/'predictions/ranking_results.csv').open('w');rw=csv.writer(rf);rw.writerow(['query_index','method']+KEYS)
    for qi in range(n):
        candidates=np.flatnonzero(data['eligible'][qi]).tolist()
        if not candidates:continue
        query=data['query_time'][qi];sid=sids[int(data['session'][qi])];recency=np.zeros(A);known=np.zeros(A,bool)
        for a in candidates:
            if a in past[sid]:
                times,exits=past[sid][a];pos=bisect.bisect_right(times,query)-1
                if pos>=0:
                    # A background candidate has already left. At same-timestamp
                    # transitions, zero is the conservative recency score.
                    recency[a]=max(0.,query-exits[pos]);known[a]=True
            missing_recency+=not known[a]
        score0=recency;score1=1-m0p[qi,:,1].astype(np.float64);score2=m1p[qi].astype(np.float64)@np.arange(8,dtype=np.float64)
        result=query_metrics(candidates,[score0,score1,score2],data['remaining'][qi],data['censored'][qi],data['observed_s'][qi])
        for mi,r in enumerate(result):
            for k in KEYS:values[qi,mi,KEYS.index(k)]=r[k]
            rw.writerow([qi,NAMES[mi]]+[r[k] for k in KEYS])
        for a in candidates:
            cls=int(m1p[qi,a].argmax());true=int(data['labels'][qi,a])
            if data['label_valid'][qi,a]:confusion[true,cls]+=1
            pw.writerow([qi,iso(query),inv[a],*m1p[qi,a].tolist(),cls,float(score2[a]),float(recency[a]),bool(known[a]),float(score1[a]),true,
                float(data['remaining'][qi,a]) if not data['censored'][qi,a] else '',bool(data['censored'][qi,a]),bool(data['label_valid'][qi,a]),int(oi[qi])])
        if qi%10000==0:write(out/'progress.json',{'phase':'offline metrics','completed':qi,'queries':n})
    pf.close();rf.close();values.flush()
    reports={scope:summarize(values,selection) for scope,selection in [('all_nonempty',counts>0),('multi_candidate',counts>=2),('original_M0_queries',(counts>0)&(oi>=0)),('original_M0_multi',(counts>=2)&(oi>=0))]}
    support=confusion.sum(1);predicted=confusion.sum(0);tp=confusion.diagonal();pr=np.divide(tp,predicted,out=np.zeros(8),where=predicted>0);rc=np.divide(tp,support,out=np.zeros(8),where=support>0);f1=np.divide(2*pr*rc,pr+rc,out=np.zeros(8),where=(pr+rc)>0)
    classification={'accuracy':float(tp.sum()/confusion.sum()),'macro_f1_all_8':float(f1.mean()),'per_class':[{'class':k,'support':int(support[k]),'precision':float(pr[k]),'recall':float(rc[k]),'f1':float(f1[k])} for k in range(8)]}
    write(out/'metrics/classification.json',classification);np.savetxt(out/'metrics/confusion_matrix.csv',confusion,fmt='%d',delimiter=',')
    g=gate(values,counts>=2,data['user'],cfg);write(out/'metrics/gate1.json',g)
    write(out/'metrics/offline.json',{'scopes':reports,'classification':classification,'gate1':g,'recency_unknown_candidate_pairs':missing_recency,
        'recency_definition':'query minus last foreground exit using all causal entries in current derived session; missing history uses score 0 conservatively, with known flag. LSTM still sees only 20.',
        'batch_inference_seconds_per_query':sum(t for t,n in lat)/sum(n for t,n in lat),'metric_keys':KEYS})
    finish_report(out,cfg,data,values,reports,classification,g,confusion)


if __name__=='__main__':main()
