import argparse
import json
import logging
import time
import uuid
from pathlib import Path
from .config import ROOT,load,save
from .asset_inspector import discover,pin,inspect
from .environment import check
from .state_machine import Machine
from .report import generate


def main(argv=None):
    parser=argparse.ArgumentParser(description='外部真实桌面 Krita GUI 自动化，不使用插件或内存采集')
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ['inspect','check-env','run']:
        p=sub.add_parser(action); p.add_argument('--config',default=str(ROOT/'configs/gui.json'))
        if action=='inspect': p.add_argument('--select-config')
        if action=='run':
            p.add_argument('--outputs',default=str(ROOT/'outputs'))
            p.add_argument('--startup-only',action='store_true',help='仅验证新 Krita PID/窗口，保留空白窗口，不打开素材')
    sub.add_parser('report').add_argument('run')
    a=parser.parse_args(argv)
    try:
        if a.action=='report': return 0 if generate(a.run)['valid'] else 1
        c=load(a.config)
        if a.action=='inspect':
            rows=discover(c); print(json.dumps(rows,ensure_ascii=False,indent=2))
            if a.select_config: print(json.dumps(pin(c,rows,a.select_config),ensure_ascii=False,indent=2))
            return 0 if len([r for r in rows if r.get('eligible')])==1 else 2
        if a.action=='check-env':
            info=check(c); print(json.dumps(info,ensure_ascii=False,indent=2)); return 0 if info['ok'] else 2
        run=Path(a.outputs).resolve()/uuid.uuid4().hex; run.mkdir(parents=True,mode=0o700)
        logging.basicConfig(filename=run/'automation.log',level=logging.INFO,force=True)
        m=dict(kind='krita_external_gui',status='NOT_RUN',success=False,run_id=run.name,
               started_wall_ns=time.time_ns(),configuration=c,asset=None,error=None,startup_only=a.startup_only)
        try:
            info=check(c); m['environment']=info
            if not info['ok']: raise RuntimeError('环境预检失败：'+'; '.join(info['errors']))
            if not c['asset']['path']:
                save(run/'asset-candidates.json',discover(c)); raise RuntimeError('尚未唯一确认并锁定原大素材，先 inspect --select-config')
            asset=inspect(c['asset']['path'])
            if asset['sha256']!=c['asset']['sha256']: raise RuntimeError('素材摘要与锁定配置不符')
            if asset['width']*asset['height']<c['minimum_pixels'] or 'smoke' in Path(asset['path']).name.lower(): raise RuntimeError('拒绝小素材替代目标')
            m['asset']=asset; save(run/'metadata.json',m)
            from .gui_controller import Controller
            gui=Controller(c,run,asset)
            Machine(gui,run,m).execute()
            m['krita_pid']=gui.pid; m['window_id']=gui.wid
        except Exception as e:
            m.update(error=repr(e),success=False,scene_preserved=True)
            logging.exception('GUI run failed')
        finally:
            m['ended_wall_ns']=time.time_ns(); save(run/'metadata.json',m)
            summary=generate(run)
            print(json.dumps(dict(run_directory=str(run),**summary),ensure_ascii=False,indent=2))
        return 0 if summary['valid'] or (a.startup_only and summary.get('startup_test')=='PASS') else 1
    except Exception as e:
        print(f'{type(e).__name__}: {e}'); return 2


if __name__=='__main__': raise SystemExit(main())
