#!/usr/bin/env python3
"""Offline threshold comparison using heldout predictions; no kernel/GUI access."""
import argparse
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PREDICTOR = ROOT / 'lzx/tool/operation_predictor'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thresholds', type=float, nargs='+', default=[.9, .8])
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if any(not .2 < t <= 1 for t in args.thresholds):
        parser.error('hot thresholds must be above .2 and at most 1')
    source = PREDICTOR / 'outputs/lsapp_expanded/visit_window_v1'
    archive = source / 'test_predictions.npz'
    checkpoint = source / 'app_lstm_visit_window.pt'
    checkpoint_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    assert checkpoint_hash == json.loads((source / 'source_hashes.json').read_text())[checkpoint.name]
    with np.load(archive, allow_pickle=False) as data:
        p, y, valid, eligible, names = [data[k] for k in
            ('probabilities', 'labels', 'valid', 'eligible', 'app_names')]
    vocab = {str(name): i for i, name in enumerate(names)}
    assert p.shape == y.shape == valid.shape == (len(eligible), len(vocab), 2)
    assert np.isfinite(p).all() and ((0 <= p) & (p <= 1)).all()
    assert (p[:, :, 0] <= p[:, :, 1]).all()
    # Verify row alignment, background eligibility and censored targets against
    # the original test partition, not against planned future GUI actions.
    rows = [json.loads(line) for line in (source / 'test_rows.jsonl').read_text().splitlines()]
    dataset = PREDICTOR / 'data/lsapp_expanded/processed/app_visit_window_v1/test.csv'
    count = 0
    with dataset.open() as stream:
        for i, row in enumerate(csv.DictReader(stream)):
            assert all(row[k] == value for k, value in rows[i].items())
            expected = {a for a in row['opened_apps'].split('|')
                        if a and a != row['current_app'] and not a.startswith('<')}
            assert set(names[eligible[i]]) == expected
            for window, seconds in enumerate((30, 180)):
                assert set(names[y[i, :, window] == 1]) == set(filter(None, row[f'labels_visit_{seconds}s'].split('|')))
                assert np.array_equal(valid[i, :, window], np.array(row[f'valid_visit_{seconds}s'].split('|'), dtype=float))
            count += 1
    assert count == len(p) == len(rows)
    module_path = PREDICTOR / 'v3/train/train_app_lstm_visit_window.py'
    spec = importlib.util.spec_from_file_location('visit_threshold_metrics', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reports = []
    for threshold in args.thresholds:
        report = module.metrics(p, y, valid, eligible, vocab, hot=threshold, cold=.2)
        selected = eligible & (p[:, :, 0] >= threshold)
        report['thermal'].update(
            anchors_with_hot=int(selected.any(axis=1).sum()),
            mean_hot_apps_per_anchor=float(selected.sum(axis=1).mean()),
            max_hot_apps_per_anchor=int(selected.sum(axis=1).max()))
        reports.append(report)
    report = {'split': 'test', 'prediction_source': str(archive),
              'prediction_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'checkpoint_sha256': checkpoint_hash, 'dataset_alignment_verified': True,
              'samples': count, 'max_background_p30': float(p[:, :, 0][eligible].max()),
              'results': reports,
              'interpretation': 'Post-hoc test threshold comparison; no retraining or performance experiment.'}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['# 独立测试集热应用阈值比较', '',
             f'固定 checkpoint，测试锚点 {count} 个。候选仅为已打开的后台真实应用；未知标签排除。',
             f'候选最高 p30：{report["max_background_p30"]:.8f}。', '',
             '| 阈值 | 选出应用次数 | 有效标签数 | 精确率 | 召回率 | 候选覆盖率 | 有热应用的锚点 |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for result in reports:
        t = result['thermal']
        pct = lambda value: 'N/A' if value is None else f'{value:.4%}'
        lines.append(f'| {t["hot_threshold"]:.2f} | {t["hot_selected"]} | {t["hot_valid_labels"]} | {pct(t["hot_precision"])} | {pct(t["hot_recall"])} | {pct(t["hot_coverage"])} | {t["anchors_with_hot"]} |')
    lines += ['', '无预测热应用时精确率无定义，不能写作 0% 或 100%。',
              '仅调整分类阈值，原始概率、PR-AUC、Brier 和冷应用规则不变。',
              '本次是测试集上的事后比较；进一步选择阈值应使用验证集。未运行 GUI 或内核性能实验。']
    (args.output_dir / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({**{k: v for k, v in report.items() if k != 'results'},
                      'thermal': [r['thermal'] for r in reports]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
