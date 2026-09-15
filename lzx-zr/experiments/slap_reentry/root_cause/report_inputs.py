"""Reproduce count-only report inputs; no fitting or model selection."""
from common import *


def main():
    datasets = {s: load_data(BASE / 'dataset' / s) for s in ['train', 'val', 'test']}
    users = {s: np.unique(d['user']) for s, d in datasets.items()}
    test = datasets['test']
    unseen = ~np.isin(test['user'], users['train'])
    counts = test['eligible'].sum(1)
    coverage = {
        'users_by_split': {s: len(u) for s, u in users.items()},
        'train_test_shared_users': len(np.intersect1d(users['train'], users['test'])),
        'test_users_unseen_in_training': len(np.setdiff1d(users['test'], users['train'])),
        'test_candidate_rows_unseen_users': int(counts[unseen].sum()),
        'test_candidate_rows_total': int(counts.sum()),
        'test_multi_queries_unseen_users': int(((counts >= 2) & unseen).sum()),
        'test_multi_queries_total': int((counts >= 2).sum()),
        'model_user_feature': 'single generic user_group, no user ID input',
        'interpretation': 'This measures user coverage; no user split is changed and no model is trained/selected on these subgroups.',
    }
    target = OUT / 'metrics/user_coverage.json'
    if target.exists():
        assert json.loads(target.read_text()) == coverage
    write(target, coverage)

    raw = load_data(OUT / 'dataset/original/test')
    rm = json.loads(Path(CFG['raw_meta']).read_text())
    mapping = json.loads(Path(META['identity_mapping']).read_text())
    pairs = [(META['app_vocab'][r['mapped_app']], rm['app_vocab'][r['lsapp_apps'][0]])
             for r in mapping['mapping_rules'] if len(r['lsapp_apps']) == 1]
    shared_count = np.zeros(len(raw['query_time']), dtype=np.int32)
    old_index = raw['old_query_index']
    for mapped_app, raw_app in pairs:
        shared = test['eligible'][old_index, mapped_app] & raw['eligible'][:, raw_app]
        q = np.flatnonzero(shared)
        np.testing.assert_array_equal(test['censored'][old_index[q], mapped_app], raw['censored'][q, raw_app])
        np.testing.assert_allclose(test['remaining'][old_index[q], mapped_app], raw['remaining'][q, raw_app], equal_nan=True)
        shared_count += shared
    parity = {
        'status': 'PASS', 'one_to_one_app_pairs': len(pairs),
        'shared_candidate_labels_checked': int(shared_count.sum()),
        'queries_with_at_least_two_shared_candidates': int((shared_count >= 2).sum()),
    }
    target = OUT / 'metrics/one-to-one-label-parity.json'
    if target.exists():
        assert json.loads(target.read_text()) == parity
    write(target, parity)
    print('Count-only report inputs PASS', flush=True)


if __name__ == '__main__':
    main()
