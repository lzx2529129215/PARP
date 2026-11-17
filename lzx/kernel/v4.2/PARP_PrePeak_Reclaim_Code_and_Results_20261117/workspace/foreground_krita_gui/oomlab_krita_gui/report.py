import json
from pathlib import Path
from .config import save
from .state_machine import STATES


def generate(run):
    run=Path(run).resolve(); m=json.loads((run/'metadata.json').read_text())
    rows=[json.loads(l) for l in (run/'stages.jsonl').read_text().splitlines()] if (run/'stages.jsonl').exists() else []
    valid=bool(m.get('success') and m.get('status')=='COMPLETE' and [r['stage'] for r in rows]==list(STATES)
               and all(r['success'] and r['verification'].get('ok') for r in rows)
               and rows[-1]['verification'].get('returncode')==0)
    identity_ok=(m.get('kind')=='krita_external_gui' and type(m.get('krita_pid')) is int and bool(m.get('window_id'))
                 and m.get('environment',{}).get('ok') is True and bool((m.get('asset') or {}).get('sha256')))
    startup_valid=bool(m.get('startup_only') is True and m.get('status')=='STARTUP_VERIFIED' and identity_ok
                       and [r['stage'] for r in rows]==list(STATES[:2])
                       and all(r['success'] and r['verification'].get('ok') for r in rows))
    valid=valid and identity_ok and m.get('startup_only') is not True
    missing=[]
    for row in rows:
        for key in ['screenshot_before','screenshot_after']:
            rel=row.get(key)
            if not rel or Path(rel).is_absolute() or '..' in Path(rel).parts or not (run/rel).is_file() or (run/rel).read_bytes()[:8]!=b'\x89PNG\r\n\x1a\n':
                missing.append((row['stage'],key))
            else:
                try:
                    from PIL import Image
                    with Image.open(run/rel) as image: image.verify()
                except Exception: missing.append((row['stage'],key))
    valid=valid and not missing
    startup_valid=startup_valid and not missing
    summary=dict(kind='krita_external_gui',valid=valid,status='COMPLETE' if valid else m.get('status','FAILED'),
                 gui_test='PASS' if valid else 'NOT_RUN' if not rows or startup_valid else 'FAILED_OR_UNPROVEN',
                 startup_test='PASS' if valid or startup_valid else 'NOT_RUN' if not rows else 'FAILED_OR_UNPROVEN',
                 stability_test='NOT_RUN',
                 error=m.get('error'),missing_screenshots=missing,asset=m.get('asset'),environment=m.get('environment'))
    if not valid and not startup_valid and summary['status'] in ('COMPLETE','STARTUP_VERIFIED'): summary['status']='INVALID_EVIDENCE'
    save(run/'summary.json',summary)
    text=['# Krita 真实桌面操作报告','',json.dumps(summary,ensure_ascii=False,indent=2),'',
          '只报告 GUI 自动化；没有内存峰值或 cgroup 实验。单轮不证明三轮稳定性。',
          'STARTUP_VERIFIED 只证明启动/绑定；不打开 ORA，不验证完整流程或正常退出。空白窗口保留，请核对 PID 后手动关闭。',
          '|阶段|成功|耗时秒|验证|前截图|后截图|错误|','|---|---|---:|---|---|---|---|']
    def clean(value): return str(value).replace('|','/').replace('\n',' ')
    for r in rows:
        seconds=(r['monotonic_end_ns']-r['monotonic_start_ns'])/1e9
        text.append('|'+ '|'.join(clean(x) for x in [r['stage'],r['success'],round(seconds,3),r['verification'],
                         r['screenshot_before'],r['screenshot_after'],r['error']])+'|')
    (run/'report.md').write_text('\n'.join(text)+'\n')
    return summary
