"""Isolated WPS launch regression; never a substitute for the 85-segment trial."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import traceback
import user_events_oracle as O


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--modes',nargs='+',choices=['fusion','components'],default=['fusion','components'])
    p.add_argument('--cycles',type=int,default=3);p.add_argument('--operations',action='store_true')
    p.add_argument('--clean-session',action='store_true')
    p.add_argument('--word-save',action='store_true')
    p.add_argument('--office-dir',type=Path)
    p.add_argument('--fixed-runtime',action='store_true')
    a=p.parse_args()
    if a.cycles<=0:p.error('--cycles must be positive')
    if a.word_save and not a.operations:p.error('--word-save requires --operations')
    if a.office_dir and a.fixed_runtime:p.error('Choose one runtime source')
    out=a.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    plan=O.compile_plan(O.ROOT/'test_reports/user_events原始与当前复现对比.xlsx',Path('/home/lzx/Desktop/user_events合并.xlsx'))
    results=[]
    with O.exclusive_experiment(out) as state:
        for mode in a.modes:
            dest=out/mode;dest.mkdir();f=O.U.fixtures(dest);pages=O.U.LocalPages(dest,f)
            slice_name='wpsprobe'+O.hashlib.sha256(str(dest).encode()).hexdigest()[:10]+'.slice'
            O.remember(state,unit=slice_name);O.systemctl('start',slice_name)
            result=dict(mode=mode,status='RUNNING',completed=0,started_at=time.time(),operations=a.operations,
                        plan_sha256=plan['plan_sha256'])
            O.write_json(dest/'controls.json',{p:O.read_priv(p) for p in O.CONTROL_VALUES})
            office=a.office_dir.resolve() if a.office_dir else Path('/opt/kingsoft/wps-office/office6')
            if a.fixed_runtime:office=O.prepare_runtime(dest/'wps-runtime')
            O.write_json(dest/'wps-config-evidence.json',dict(office_dir=str(office),
                oem=(office/'cfgs/oem.ini').read_text(),
                binaries={name:O.hashlib.sha256((office/name).read_bytes()).hexdigest() for name in ['wps','wpp','et','wpsoffice','wpscloudsvr']}))
            try:
                with O.U.P.desktop_api.desktop('isolated',dest):
                    O.remember_desktop(state,dest)
                    r=O.ContinuousReplay(dest,f,pages,slice_name,state);r.wps_launch_mode=mode
                    r.wps_clean_session=a.clean_session
                    r.wps_office_dir=office
                    try:
                        r.current_row=0;r.ensure('WPS')
                        if a.operations:
                            segments=[s for s in plan['segments'] if s['app_key']=='WPS' and len(s['events'])>10][:a.cycles]
                            for s in segments:
                                source=dest/f'source-{s["index"]}';source.mkdir();r.f=O.U.fixtures(source)
                                r.current_row=s['events'][0]['excel_row']
                                if s['events'][0]['operation']!='document_open':
                                    r.open_file('WPS',r.f/'word.docx')
                                    O.log(dest/'preparations.jsonl',dict(row=r.current_row,document=str(r.f/'word.docx')))
                                for e in s['events']:
                                    if a.word_save and not (e['operation'].startswith('save_as') or e['params'].get('document')=='word' or e['event1']=='点击“文件”'):continue
                                    r.current_row=e['excel_row'];detail=r.action(e)
                                    if a.word_save:r.screenshot('after')
                                    O.log(dest/'operations.jsonl',dict(row=r.current_row,detail=detail))
                                    result['completed']+=1
                                print(mode,'segment',s['index'],'operations',result['completed'],flush=True)
                        else:
                            for i in range(a.cycles):
                                for name in ['word.docx','ppt.pptx','sheet.xlsx']:
                                    r.current_row=result['completed']+1
                                    detail=r.open_file('WPS',f/name)
                                    r.key('WPS','Escape');r.key('WPS','ctrl+Home');time.sleep(1)
                                    O.log(dest/'opens.jsonl',dict(document=name,detail=detail,window=r.active('WPS')))
                                    result['completed']+=1
                                    print(mode,'open',result['completed'],name,flush=True)
                        result['status']='PASS'
                    except Exception:
                        r.screenshot('failed');raise
                    finally:result['cleanup']=r.finish()
            except Exception as exc:result.update(status='FAIL',error=repr(exc),traceback=traceback.format_exc())
            finally:
                pages.close();O.systemctl('stop',slice_name,check=False)
                result['ended_at']=time.time()
                log=subprocess.run(['sudo','-n','journalctl','-k','--since','@'+str(int(result['started_at'])),
                    '--until','@'+str(int(result['ended_at'])+1),'--no-pager'],capture_output=True,text=True,check=True).stdout
                (dest/'kernel.log').write_text(log)
                result['wps_segfaults']=[line for line in log.splitlines() if 'segfault' in line and any(x in line for x in ['wps','et[','wpp['])]
                result['crash_free']=result['status']=='PASS' and not result['wps_segfaults']
                O.write_json(dest/'result.json',result);results.append(result);O.write_json(out/'summary.json',results)
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return int(any(not r['crash_free'] for r in results))


if __name__=='__main__':raise SystemExit(main())
