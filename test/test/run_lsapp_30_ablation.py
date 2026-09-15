#!/usr/bin/env python3
"""Six offline experiments: 5/10/20 segments, with/without explicit features."""
import argparse
import bisect
import csv
import datetime as dt
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
PREDICTOR = ROOT / 'lzx/tool/operation_predictor'
sys.path.insert(0, str(PREDICTOR))
from v3.train.train_app_lstm_visit_window import read_data, evaluate, metrics, average_precision, ratio
from v3.models.app_lstm_visit_window import AppLSTMVisitWindow, masked_loss, HORIZONS, VISIT_DEFINITION
from v3.models.app_lstm_visit_explicit import AppLSTMVisitExplicit, explicit_features, FEATURE_NAMES, MODEL_TYPE


def write(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def timestamp(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc).timestamp()


def load_aligned(dataset, meta, out):
    sessions = defaultdict(list)
    with (dataset / 'segments.csv').open() as f:
        for row in csv.DictReader(f):
            sessions[row['session_id']].append((timestamp(row['start_time']),
                meta['app_vocab'][row['app']], float(row['dwell_s'])))
    for values in sessions.values():
        values.sort(key=lambda v: v[0])  # Preserve source segment order on tied timestamps.
    starts = {k: [v[0] for v in values] for k, values in sessions.items()}
    data = {}
    audits = {}
    for split in ('train', 'val', 'test'):
        write(out/'progress.json', {'state':'preparing','split':split})
        original = read_data(dataset/f'{split}.csv', meta['app_vocab'], meta['group_vocab'], 5)
        features, labels, valid, rows = original
        n = len(rows)
        apps = np.full((n,20), meta['app_vocab']['<PAD>'], dtype=np.int64)
        durations = np.zeros((n,20), dtype=np.float32)
        mask = np.zeros((n,20), dtype=np.float32)
        ages = np.zeros((n,20), dtype=np.float64)
        lengths = []
        cursors = {}
        for i, row in enumerate(rows):
            anchor = timestamp(row['timestamp'])
            values = sessions[row['session_id']]
            times = starts[row['session_id']]
            left, right = bisect.bisect_left(times, anchor), bisect.bisect_right(times, anchor)
            candidates = range(left, right) if left < right else [right-1]
            previous_anchor, previous_idx = cursors.get(row['session_id'], (None, -1))
            expected = features['history_apps'][i][features['history_mask'][i].bool()].tolist()
            matches = [j for j in candidates if j >= 0
                and (previous_anchor != anchor or j > previous_idx)
                and [v[1] for v in values[max(0,j-4):j+1]] == expected]
            assert matches, f'cannot align causal history: {split} row {i}'
            idx = matches[0]
            cursors[row['session_id']] = (anchor, idx)
            history = values[max(0, idx-19):idx+1]
            count = len(history)
            apps[i,-count:] = [v[1] for v in history]
            # Preserve legacy transition dwell floor=1 for every sequence arm.
            # Explicit ages below use true time and never the final current dwell.
            durations[i,-count:] = [v[2] for v in history[:-1]] + [max(1,anchor-history[-1][0])]
            ages[i,-count:] = [v[0]-anchor for v in history]
            mask[i,-count:] = 1
            lengths.append(idx+1)
        for key, rebuilt in [('history_apps',apps), ('history_durations',durations), ('history_mask',mask)]:
            assert np.array_equal(rebuilt[:,-5:], features[key].numpy()), f'{split}: original five-segment mismatch in {key}'
        data[split] = (original, apps, durations, mask, ages)
        audits[split] = {'rows':n, 'original_five_segment_inputs_identical':True,
            'labels_masks_open_set_and_anchor_order_reused_exactly':True,
            'anchors_with_more_than_5_segments':int((np.array(lengths)>5).sum()),
            'anchors_with_more_than_10_segments':int((np.array(lengths)>10).sum()),
            'anchors_with_at_least_20_segments':int((np.array(lengths)>=20).sum())}
        print('loaded',split,audits[split],flush=True)
    write(out/'data-audit.json',audits)
    return data


def background_metrics(p, y, valid, eligible):
    result = {}
    for index, window in enumerate((30,180)):
        known = eligible & valid[:,:,index].astype(bool)
        target, score = y[:,:,index][known], p[:,:,index][known]
        result[str(window)] = {'valid_labels':int(known.sum()),'positives':int(target.sum()),
            'pr_auc':average_precision(target,score),
            'brier':float(np.mean((target-score)**2)) if len(target) else None}
    ranking = np.argsort(-np.where(eligible,p[:,:,0],-1),axis=1,kind='stable')
    for k in (1,2):
        selected = np.zeros_like(eligible)
        np.put_along_axis(selected,ranking[:,:k],True,axis=1)
        selected &= eligible
        known = selected & valid[:,:,0].astype(bool)
        hits = known & (y[:,:,0] == 1)
        result[f'top{k}'] = {'selected':int(selected.sum()),'valid_selected':int(known.sum()),
            'precision':ratio(hits.sum(),known.sum()),
            'recall':ratio(hits.sum(),(eligible & valid[:,:,0].astype(bool) & (y[:,:,0]==1)).sum())}
    result['max_p30'] = float(p[:,:,0][eligible].max())
    return result


def summarize(out, results):
    val_best = max(results, key=lambda r: r['splits']['val']['background']['30']['pr_auc']) if results else None
    write(out/'summary.json', {'results':results,'preferred_by_validation_background_30s_ap':val_best['name'] if val_best else None,
        'selection_note':'Epoch chosen by validation loss; model comparison by validation background AP. Test not used for selection.'})
    lines = ['# 历史长度与显式特征离线消融', '',
        '六组相同锚点、标签、时间划分、打开集合、优化参数及随机种子；每组20轮，验证损失选择checkpoint。',
        '显式特征只计算当前组可见的历史；缺失时间有掩码，频次附观察覆盖标记。',
        '序列延续旧切入时长最小1秒约定；显式时间差使用真实时间。无在线部署或内核实验。', '',
        '| 组别 | 最佳epoch | 验证后台30s AP | 测试后台30s AP | 测试后台30s Brier | Top1精确率 | Top1召回率 | 0.8热选出数 | 冷实际访问率 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in results:
        v,t = r['splits']['val'],r['splits']['test']; b=t['background']; th=t['thermal']
        lines.append(f'| {r["name"]} | {r["best_epoch"]} | {v["background"]["30"]["pr_auc"]:.4f} | {b["30"]["pr_auc"]:.4f} | {b["30"]["brier"]:.5f} | {b["top1"]["precision"]:.2%} | {b["top1"]["recall"]:.2%} | {th["hot_selected"]} | {th["cold_actual_visit_rate"]:.2%} |')
    lines += ['', 'AP越高越好，Brier越低越好；Top1允许低置信度候选，仅用于排序诊断，不等价于热应用。',
        '冷访问率须结合冷名单覆盖率比较，完整180秒指标、0.8/0.9热指标及有效样本数见summary.json。',
        '单随机种子消融，不构成重复实验显著性证据。旧测试集已被用于事后诊断，最终推广需新的时间留出验证。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--epochs',type=int,default=20)
    parser.add_argument('--threads',type=int,default=2)
    args=parser.parse_args()
    torch.set_num_threads(args.threads)
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    dataset=args.dataset.resolve()
    assert len(json.loads((dataset/'dataset_meta.json').read_text())['app_vocab'])==32
    meta=json.loads((dataset/'dataset_meta.json').read_text())
    model_args=dict(num_apps=len(meta['app_vocab']), num_user_groups=max(meta['group_vocab'].values())+1,
                    pad_id=meta['app_vocab']['<PAD>'],duration_cap_s=600.)
    settings={'epochs':args.epochs,'batch_size':2048,'learning_rate':.001,'seed':42,'threads':args.threads,
        'histories':[5,10,20],'explicit':[False,True],'device':'cpu','feature_names':FEATURE_NAMES,
        'checkpoint_selection':'validation_masked_loss','model_selection':'validation_background_30s_ap',
        'candidate_definition':'opened_nonforeground_real_apps','explicit_scope':'same_truncated_history_only',
        'feature_normalization':{'recency_cap_s':3600,'dwell_cap_s':600,'count_divisor':20},
        'dataset':str(dataset),'real_app_count':30,'legacy_transition_dwell_floor_s':1,'explicit_transition_age_s':0}
    sources=[Path(__file__),dataset/'dataset_meta.json',Path(__file__).with_name('prepare_lsapp_30.py'),PREDICTOR/'v3/models/app_lstm_visit_explicit.py',
             PREDICTOR/'v3/models/app_lstm_visit_window.py',PREDICTOR/'v3/models/app_lstm_duration.py',
             PREDICTOR/'v3/train/train_app_lstm_visit_window.py']+[dataset/f'{s}.csv' for s in ('train','val','test','segments')]
    settings['source_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    write(out/'experiment.json',settings)
    all_data=load_aligned(dataset,meta,out)
    results=[]
    for history_len in settings['histories']:
        datasets={}
        explicit_tensors={}
        for split,(original,apps,durations,mask,ages) in all_data.items():
            write(out/'progress.json',{'state':'building_explicit_features','history_len':history_len,'split':split,'completed':len(results),'total':6})
            base,labels,valid,rows=original
            features={**base,'history_apps':torch.from_numpy(apps[:,-history_len:].copy()),
                'history_durations':torch.from_numpy(durations[:,-history_len:].copy()),
                'history_mask':torch.from_numpy(mask[:,-history_len:].copy())}
            ex=np.zeros((len(rows),model_args['num_apps'],len(FEATURE_NAMES)),np.float32)
            for i in range(len(rows)):
                valid_i=mask[i,-history_len:].astype(bool)
                ex[i]=explicit_features(apps[i,-history_len:][valid_i].tolist(),
                    ages[i,-history_len:][valid_i].tolist(),0.,model_args['num_apps'])
            datasets[split]=(features,labels,valid,rows)
            explicit_tensors[split]=torch.from_numpy(ex)
        for explicit in settings['explicit']:
            name=f'h{history_len}_'+('explicit' if explicit else 'plain')
            arm=out/name;arm.mkdir()
            torch.manual_seed(42);np.random.seed(42)
            model=(AppLSTMVisitExplicit if explicit else AppLSTMVisitWindow)(**model_args)
            torch.manual_seed(42)  # Same dropout RNG start, independent of added parameters.
            order_rng=torch.Generator().manual_seed(42)
            current={s:({**d[0],**({'explicit':explicit_tensors[s]} if explicit else {})},*d[1:]) for s,d in datasets.items()}
            optimizer=torch.optim.Adam(model.parameters(),lr=.001)
            features,labels,valid,_=current['train']
            started=time.monotonic();best=float('inf');history=[]
            for epoch in range(1,args.epochs+1):
                write(out/'progress.json',{'state':'training','arm':name,'epoch':epoch,'epochs':args.epochs,'completed':len(results),'total':6})
                model.train();order=torch.randperm(len(labels),generator=order_rng);total=0.;batches=0
                for start in range(0,len(labels),2048):
                    idx=order[start:start+2048]
                    logits=model(**{k:v[idx] for k,v in features.items()})
                    loss=masked_loss(logits,labels[idx],valid[idx])
                    if not torch.isfinite(loss):raise ValueError('nonfinite loss')
                    optimizer.zero_grad(set_to_none=True);loss.backward();optimizer.step()
                    total+=loss.item();batches+=1
                val_loss,_=evaluate(model,current['val'],2048,'cpu')
                row={'epoch':epoch,'train_loss':total/batches,'val_loss':val_loss,'elapsed_s':time.monotonic()-started}
                history.append(row);write(arm/'training_history.json',history)
                print(name,json.dumps(row),flush=True)
                if val_loss<best:
                    best=val_loss
                    checkpoint={'model_type':MODEL_TYPE if explicit else 'app_visit_window_v1',
                        'schema_version':2 if explicit else 1,'prediction_format':'visit_window',
                        'horizons_s':list(HORIZONS),'visit_definition':VISIT_DEFINITION,
                        'probability_parameterization':'p30_plus_survival30_times_sigmoid_z2',
                        'model_args':model_args,'history_len':history_len,'explicit_features':explicit,
                        'feature_names':FEATURE_NAMES if explicit else [],'experiment':settings,
                        'app_vocab':meta['app_vocab'],'group_vocab':meta['group_vocab'],
                        'best_epoch':epoch,'validation_loss':best,'model_state_dict':model.state_dict()}
                    torch.save(checkpoint,arm/'checkpoint.pt')
            ckpt=torch.load(arm/'checkpoint.pt',weights_only=False,map_location='cpu')
            model.load_state_dict(ckpt['model_state_dict'])
            result={'name':name,'history_len':history_len,'explicit':explicit,'best_epoch':ckpt['best_epoch'],
                    'training_seconds':time.monotonic()-started,'parameter_count':sum(v.numel() for v in model.parameters()),'splits':{}}
            for split in ('val','test'):
                loss,p=evaluate(model,current[split],2048,'cpu',True)
                f,y,v,_=current[split];y=y.numpy();v=v.numpy()
                eligible=f['opened_apps'].numpy().astype(bool)
                eligible[np.arange(len(eligible)),f['current_app'].numpy()]=False
                for app,aid in meta['app_vocab'].items():
                    if app.startswith('<'):eligible[:,aid]=False
                report=metrics(p,y,v,eligible,meta['app_vocab'],hot=.8,cold=.2)
                report['hot90']=metrics(p,y,v,eligible,meta['app_vocab'],hot=.9,cold=.2)['thermal']
                report['background']=background_metrics(p,y,v,eligible);report['loss']=loss
                assert report['monotonicity_violations']==0
                result['splits'][split]=report
                np.savez_compressed(arm/f'{split}_predictions.npz',probabilities=p,labels=y,valid=v,eligible=eligible,
                    app_names=np.array(list(meta['app_vocab'])))
            write(arm/'evaluation.json',result)
            results.append(result);summarize(out,results)
            print('completed',name,flush=True)
    write(out/'progress.json',{'state':'complete','completed':6,'total':6})


if __name__=='__main__':
    main()
