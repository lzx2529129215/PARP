#!/usr/bin/env python3
"""Select a trained arm and high-recall thresholds using validation data only."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import numpy as np
import torch


def fraction(a, b):
    return float(a / b) if b else None


def threshold_for_recall(data, target):
    positive = data['eligible'] & data['valid'][:, :, 0].astype(bool) & (data['labels'][:, :, 0] == 1)
    scores = np.sort(data['probabilities'][:, :, 0][positive])[::-1]
    if not len(scores):
        raise ValueError('No validation background positives')
    # Include ties: the highest threshold that retains at least ceil(target*N) positives.
    return float(scores[int(np.ceil(target * len(scores))) - 1])


def policy(data, threshold):
    p, y, v, eligible = (data[k] for k in ('probabilities', 'labels', 'valid', 'eligible'))
    valid30 = v[:, :, 0].astype(bool)
    positives = eligible & valid30 & (y[:, :, 0] == 1)
    hot = eligible & (p[:, :, 0] >= threshold)
    known = hot & valid30
    hits = known & (y[:, :, 0] == 1)
    cold = eligible & ~hot & (p[:, :, 1] < .2)
    cold_known = cold & v[:, :, 1].astype(bool)
    sizes = hot.sum(1)
    nonempty = eligible.any(1)
    multi = eligible.sum(1) >= 2
    positive_rows = positives.any(1)
    return {
        'threshold': threshold, 'candidate_pairs': int(eligible.sum()),
        'valid_positive_pairs': int(positives.sum()),
        'hot_selected': int(hot.sum()), 'hot_valid_selected': int(known.sum()),
        'hot_hits': int(hits.sum()), 'hot_precision': fraction(hits.sum(), known.sum()),
        'hot_recall': fraction(hits.sum(), positives.sum()),
        'missed_positive_pairs': int(positives.sum() - hits.sum()),
        'multi_candidate_recall': fraction(hits[multi].sum(), positives[multi].sum()),
        'multi_candidate_precision': fraction(hits[multi].sum(), known[multi].sum()),
        'all_returns_selected_anchor_rate': fraction(((hits.sum(1) == positives.sum(1)) & positive_rows).sum(), positive_rows.sum()),
        'hot_pair_coverage': fraction(hot.sum(), eligible.sum()),
        'hot_anchor_coverage_among_nonempty': fraction((sizes > 0).sum(), nonempty.sum()),
        'hot_mean_size_all_anchors': float(sizes.mean()),
        'hot_mean_size_nonempty_candidates': float(sizes[nonempty].mean()),
        'hot_size_histogram': {str(int(k)): int(n) for k, n in zip(*np.unique(sizes, return_counts=True))},
        'cold_selected': int(cold.sum()), 'cold_valid_selected': int(cold_known.sum()),
        'cold_actual_visit_180s_rate': fraction((cold_known & (y[:, :, 1] == 1)).sum(), cold_known.sum()),
        'cold_pair_coverage': fraction(cold.sum(), eligible.sum()),
    }


def pct(x):
    return '无有效样本' if x is None else f'{x:.2%}'


def app_recall(data, threshold):
    result = {}
    for i, name in enumerate(data['app_names']):
        if str(name).startswith('<'):
            continue
        known = data['eligible'][:, i] & data['valid'][:, i, 0].astype(bool)
        positive = known & (data['labels'][:, i, 0] == 1)
        selected = data['eligible'][:, i] & (data['probabilities'][:, i, 0] >= threshold)
        hits = selected & positive
        result[str(name)] = {'known_candidate_pairs': int(known.sum()),
            'positive_pairs': int(positive.sum()), 'hot_selected': int(selected.sum()),
            'recall': fraction(hits.sum(), positive.sum()),
            'precision': fraction(hits.sum(), (selected & known).sum())}
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--training-dir', required=True, type=Path)
    ap.add_argument('--vocab', required=True, type=Path)
    args = ap.parse_args()
    root = args.training_dir
    progress = json.loads((root / 'progress.json').read_text())
    assert progress['state'] == 'complete', 'Training must finish before final selection'
    summary = json.loads((root / 'summary.json').read_text())
    selected = summary['preferred_by_validation_background_30s_ap']
    arm = root / selected
    ckpt = torch.load(arm / 'checkpoint.pt', map_location='cpu', weights_only=False)
    vocab = json.loads(args.vocab.read_text())
    assert ckpt['app_vocab'] == vocab and len(vocab) == 32
    assert ckpt['horizons_s'] == [30, 180]
    assert ckpt['prediction_format'] == 'visit_window'
    assert ckpt['probability_parameterization'] == 'p30_plus_survival30_times_sigmoid_z2'
    assert all(torch.isfinite(x).all() for x in ckpt['model_state_dict'].values())
    datasets = {}
    for split in ('val', 'test'):
        with np.load(arm / f'{split}_predictions.npz') as data:
            datasets[split] = {k: data[k] for k in data.files}
        p = datasets[split]['probabilities']
        assert p.shape[1:] == (32, 2)
        assert np.isfinite(p).all() and (p >= 0).all() and (p <= 1).all()
        assert (p[:, :, 0] <= p[:, :, 1]).all()
        assert not datasets[split]['eligible'][:, 30:].any()
    thresholds = {'fixed_0.9': .9, 'fixed_0.8': .8}
    thresholds.update({f'validation_recall_{target:.0%}': threshold_for_recall(datasets['val'], target)
                       for target in (.90, .95, .99)})
    policies = {name: {s: policy(d, t) for s, d in datasets.items()} for name, t in thresholds.items()}
    evaluation = json.loads((arm / 'evaluation.json').read_text())
    background = evaluation['splits']['test']['background']
    meta = json.loads((Path(ckpt['experiment']['dataset']) / 'dataset_meta.json').read_text())
    counts = {s: meta['splits'][s]['samples'] for s in ('train', 'val', 'test')}
    candidate_counts = datasets['test']['eligible'].sum(1)
    report = {'selected_arm': selected, 'selection': 'validation background 30s AP; epoch by validation masked loss',
              'policy_note': 'Thresholds selected on validation only. Hot takes precedence over cold. Unknown labels excluded.',
              'policies': policies, 'evaluation': evaluation,
              'test_per_app_validation95_policy': app_recall(datasets['test'], thresholds['validation_recall_95%']),
              'checkpoint_sha256': hashlib.sha256((arm / 'checkpoint.pt').read_bytes()).hexdigest()}
    (root / 'high-recall-evaluation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    shutil.copy2(arm / 'checkpoint.pt', root / 'selected_checkpoint.pt')
    lines = ['# 30 应用重新训练及高召回评估', '',
             f'按验证集后台 30 秒 AP 选择 **{selected}**，最佳 epoch {ckpt["best_epoch"]}。六组均训练 20 轮，batch size 2048，单随机种子。', '',
             '30 个实际应用及两个特殊 token；特殊项不进入冷热名单。访问标签使用原始事件时间，窗口为 (t,t+30s] 与 (t,t+180s]；持续停留不算进入。', '',
             f'数据共 {sum(counts.values()):,} 个锚点：训练 {counts["train"]:,}、验证 {counts["val"]:,}、测试 {counts["test"]:,}。测试有候选锚点 {int((candidate_counts>0).sum()):,}，其中至少两个候选的锚点 {int((candidate_counts>=2).sum()):,}。', '',
             '| 测试后台窗口 | PR-AUC（AP） | Brier | 有效应用-时刻对 | 正样本对 |',
             '|---|---:|---:|---:|---:|',
             *[f'| {w}s | {background[w]["pr_auc"]:.4f} | {background[w]["brier"]:.5f} | {background[w]["valid_labels"]} | {background[w]["positives"]} |' for w in ('30', '180')], '',
             '| 策略 | 阈值 | 验证召回率 | 测试精确率 | 测试召回率 | 多候选测试召回率 | 有候选时平均热应用数 | 热候选覆盖率 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, values in policies.items():
        v, t = values['val'], values['test']
        lines.append(f'| {name} | {t["threshold"]:.6f} | {pct(v["hot_recall"])} | {pct(t["hot_precision"])} | {pct(t["hot_recall"])} | {pct(t["multi_candidate_recall"])} | {t["hot_mean_size_nonempty_candidates"]:.4f} | {pct(t["hot_pair_coverage"])} |')
    lines += ['', '阈值越低，通常漏选越少，但误保护和热名单大小会增加。验证集达到目标召回率并不保证测试集或真实 PC 达到同一目标。', '',
              '上述召回率仅针对“已打开集合中的后台候选”，不包括被候选集合排除的返回；LSApp 的打开/关闭状态不等同于 PC 进程驻留。沿用当前 builder 的片段开始打开集合，周期采样未重建片段内每次状态变化。', '',
              '新映射保留了更多原始应用之间的切换，但 30 应用与旧 15 应用任务粒度及分区锚点不同，不能将两次 AP 差值直接解释成受控精度提升。六组内部的锚点、划分、标签和候选集合完全一致。', '',
              '测试集与先前诊断使用相同 LSApp 来源，因此仍需新的时间留出或真实焦点日志确认泛化效果。这里不宣称已达到“真实场景全部返回都选中”。', '',
              '交付 selected_checkpoint.pt；完整两窗口 AP、Brier、Top1/2、固定阈值冷热指标及有效样本数见 summary.json 和 high-recall-evaluation.json。未替换在线模型、重启常驻服务或开展内核性能实验。']
    coverage_path = root / 'candidate-coverage.json'
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_text())['splits']['test']
        lines += ['', f"候选覆盖审计：测试集曾进入应用的后台 30 秒返回共 {coverage['previously_seen_background_positive_pairs']} 对，其中候选内 {coverage['eligible_positive_pairs']} 对，候选覆盖代理上限 {coverage['candidate_coverage_ceiling_proxy']:.2%}。包含关闭重启，不可解读为真实驻留后台应用的召回率上限。详细定义及逐应用计数见 candidate-coverage.json。"]
    verification_path = root / 'checkpoint-verification.json'
    if verification_path.exists():
        verification = json.loads(verification_path.read_text())
        lines += ['', f"检查点复验：严格加载参数，从原始片段时间重建 {verification['replayed_rows']} 条输入，概率最大误差 {verification['max_probability_difference']:.3g}；有限性和 p30≤p180 检查通过。输出示例见 prediction-example.json。"]
    (root / 'RETRAINING-REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'selected_arm': selected, 'test_background': evaluation['splits']['test']['background'],
                      'policies': {k: v['test'] for k, v in policies.items()}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
