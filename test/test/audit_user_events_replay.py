#!/usr/bin/env python3
"""Read-only audit of workbook coverage, saved outputs and process cleanup."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import zipfile

def audit(out):
    plan=json.loads((out/'mapping/plan.json').read_text())
    expected={e['excel_row']:e for e in plan['events']}
    results=[];errors=[];artifacts=[];groups=[]
    source=Path(plan['source'])
    if hashlib.sha256(source.read_bytes()).hexdigest()!=plan['source_sha256']:
        errors.append('Source workbook changed')
    for group in plan['groups']:
        root=out/f'group-{group["index"]:02d}'
        if not (root/'summary.json').exists():errors.append(f'Missing group {group["index"]}');continue
        rows=[json.loads(line) for line in (root/'results.jsonl').read_text().splitlines()]
        wanted=[e['excel_row'] for e in plan['events'] if e['group_index']==group['index']]
        if [r['excel_row'] for r in rows]!=wanted:errors.append(f'Incomplete/reordered group {group["index"]}')
        summary=json.loads((root/'summary.json').read_text())
        cleanup=all(item['passed'] and not item['remaining'] for item in summary['cleanup'].values())
        if not cleanup:errors.append(f'Process cleanup failed in group {group["index"]}')
        for p in root.glob('*/audit-error.json'):errors.append(str(p.relative_to(out)))
        groups.append(dict(index=group['index'],rows=len(rows),cleanup_passed=cleanup,counts=dict(Counter(r['status'] for r in rows))))
        for row in rows:
            original=expected[row['excel_row']]
            for key in ['source_dataset_id','app_name','event1','operation','target_app_key','runtime_app_id']:
                if row[key]!=original[key]:errors.append(f'Row {row["excel_row"]}: {key} changed')
            if row['status']=='SKIPPED' and original['disposition']!='skip':errors.append(f'Unexpected skip at {row["excel_row"]}')
            if row['status']=='VERIFIED' and row.get('verification') in {'none','owned_window_input_only'}:
                errors.append(f'False verified status at {row["excel_row"]}')
            if row.get('verification')=='scroll_offset':
                if (row['after']['y']-row['before']['y'])*original['params']['direction']<=0:
                    errors.append(f'Wrong scroll direction at {row["excel_row"]}')
            if row['status']=='VERIFIED' and row.get('verification') in {'saved_file','saved_note_content','native_command_artifact','export_media','recording_media'}:
                p=Path(row['path'])
                if not p.is_file() or p.stat().st_size==0:errors.append(f'Missing output at {row["excel_row"]}');continue
                check=dict(excel_row=row['excel_row'],path=str(p.relative_to(out)),bytes=p.stat().st_size,
                           sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                if p.suffix=='.docx':
                    with zipfile.ZipFile(p) as z:
                        check['word_content_present']=b'Perf_WPS_0040' in z.read('word/document.xml')
                    if not check['word_content_present']:errors.append('Saved Word content missing')
                if p.suffix=='.xcf':
                    check['xcf_header']=p.read_bytes().startswith(b'gimp xcf ')
                    if not check['xcf_header']:errors.append('Invalid XCF output')
                artifacts.append(check)
        sheet=root/'fixtures/sheet.xlsx'
        filter_rows=[r['excel_row'] for r in rows if r['operation']=='filter' and r['status'] in {'INPUT_SENT','VERIFIED'}]
        if filter_rows:
            with zipfile.ZipFile(sheet) as z:
                present=any(b'autoFilter' in z.read(n) for n in z.namelist() if n.startswith('xl/worksheets/') and n.endswith('.xml'))
            artifacts.append(dict(excel_rows=filter_rows,path=str(sheet.relative_to(out)),saved_auto_filter=present))
            if not present:errors.append('Saved sheet missing filter: '+str(sheet.relative_to(out)))
        results.extend(rows)
    if len(results)!=len(expected):errors.append('Total row count mismatch')
    return dict(source_sha256=plan['source_sha256'],expected_rows=len(expected),processed_rows=len(results),
                audit_passed=not errors,errors=errors,counts=dict(Counter(r['status'] for r in results)),groups=groups,artifacts=artifacts,
                note='Audit pass verifies complete accounting, the listed outputs and cleanup; it does not turn INPUT_SENT into business-result verification.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output_dir',type=Path);args=parser.parse_args()
    result=audit(args.output_dir.resolve())
    (args.output_dir/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['audit_passed','processed_rows','counts','errors']},ensure_ascii=False))
    raise SystemExit(0 if result['audit_passed'] else 1)
