import importlib.util
import json
from pathlib import Path

script = Path(__file__).resolve().with_name('scenario_accuracy_eval.py')
spec = importlib.util.spec_from_file_location('scenario_eval', script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = [mod.evaluate_case(case) for case in mod.SCENARIOS]
summary = {
    'scenario_count': len(results),
    'classification_accuracy': round(sum(1 for r in results if r['classification_correct']) / len(results) * 100, 2),
    'rule_trend_prediction_accuracy': round(sum(1 for r in results if r['rule_trend_correct']) / len(results) * 100, 2),
    'second_order_markov_prediction_accuracy': round(sum(1 for r in results if r['second_order_markov_correct']) / len(results) * 100, 2),
    'results': results,
}

report_dir = Path(__file__).resolve().parents[1] / 'test-reports'
report_dir.mkdir(parents=True, exist_ok=True)
report_path = report_dir / 'verify_accuracy_result.json'
report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(str(report_path))
