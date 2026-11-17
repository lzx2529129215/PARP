from pathlib import Path
import json
from execution_contract import sha

ROOT=Path(__file__).resolve().parent


def make_inputs(scenario,purpose,scope):
    if scenario not in ('stable','turnover','revisit'):raise ValueError('one matching scene required')
    if purpose not in ('calibration','formal'):raise ValueError('explicit service purpose required')
    if scope not in ('per_app','all_apps'):raise ValueError('explicit total-fault scope required')
    index=ROOT/'results'/('REFERENCE-'+scenario+'.json')
    if not index.exists():raise ValueError('a fresh reference under this test limit is required')
    item=json.loads(index.read_text(encoding='utf-8'))
    path=(ROOT/item['path']).resolve()
    if not path.is_relative_to(ROOT)or sha(path)!=item['sha256']:raise ValueError('fresh reference evidence changed')
    return dict(aligned=True,purpose=purpose,scope=scope,reference_path=str(path),
                reference_mode=item.get('reference_mode','same_limit'),
                minimum_initial_gap_bytes=item.get('minimum_initial_gap_bytes',0))
