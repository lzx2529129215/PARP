"""Krita-only input-to-visible probe; startup/OCR and intentional gaps excluded."""
import argparse
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
from harness_contracts import wait_until_ns

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1]
LAB = WORK/'krita-vm-acceptance'
sys.path.insert(0, str(WORK/'oracle-realapps-validation-20261110'))
from interaction_gui import Probe
from peak_gui import LocalProcess, FastView,identity,qt_environment
from oomlab_krita_gui.asset_inspector import inspect
from oomlab_krita_gui.config import load, save
from oomlab_krita_gui.gui_controller import Controller


class AssetProcess(LocalProcess):
    def __init__(self,run,template,asset):
        super().__init__(run,template);self.asset=asset
    def launch(self):
        env,audit=qt_environment()
        for key,name in (('HOME','private-home'),('XDG_CONFIG_HOME','private-config'),
                         ('XDG_DATA_HOME','private-data'),('XDG_CACHE_HOME','private-cache')):
            destination=self.run/name;source=self.template/name
            if source.exists():shutil.copytree(source,destination)
            else:destination.mkdir(mode=0o700)
            env[key]=str(destination)
        for key in ('PYTHONPATH','PYTHONHOME','OOM_KRITA_RUN'):env.pop(key,None)
        self.log=(self.run/'krita.log').open('xb')
        self.child=subprocess.Popen(['python3','-B',str(LAB/'cg_exec.py'),'foreground','--','krita',str(self.asset)],
            env=env,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            self.info=identity(self.child.pid)
            if Path(self.info['exe']).name=='krita':break
            if self.child.poll() is not None:raise RuntimeError('Krita native file launch failed')
            time.sleep(.02)
        else:raise TimeoutError('native Krita exec')
        save(self.run/'process.json',self.info);save(self.run/'qt-environment.json',audit)
        return self.info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--asset', type=Path, required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=26)
    args = parser.parse_args()
    args.run.mkdir()
    config = load(WORK/'foreground_krita_gui/configs/gui.json')
    config['timeouts']['load'] = 240
    config['poll_s'] = .2
    config['labels']['discard'] = ['No']
    asset = inspect(args.asset)
    g = Controller(config, args.run, asset, process=AssetProcess(args.run, args.template,args.asset))
    result = dict(success=False, intended_gaps_in_latency=False)
    try:
        g.launch_krita()
        g.verify_krita()
        subprocess.run(['wmctrl','-ir',g.wid,'-b','add,maximized_vert,maximized_horz'], check=True)
        g.mouse.PAUSE = .01
        view = FastView(g, args.run/'actions.jsonl')
        view.document(asset, 'loaded')
        result['document_verified'] = g.verify_document()
        subprocess.run(['xdotool','windowactivate','--sync',g.wid], check=True)
        time.sleep(.4)
        probe = Probe(g, g.wid)
        sequence = ('zoom_in','zoom_out','pan_left','pan_right')
        for kind in sequence:
            row = probe.action(kind, time.monotonic_ns())
            if row['timeout']:
                raise RuntimeError('foreground smoke did not visibly respond: '+kind)
        save(args.run/'ready.json', dict(krita_pid=g.pid, krita_wid=g.wid))
        deadline = time.monotonic()+180
        while not (args.run/'go').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('foreground go')
            time.sleep(.03)
        start = time.monotonic_ns()
        save(args.run/'phase-start.json', dict(monotonic_ns=start, wall_ns=time.time_ns()))
        index = 0
        action_index = 0
        with (args.run/'responses.jsonl').open('a') as output:
            while time.monotonic_ns() < start+int(args.duration*1e9):
                due = start+index*500_000_000
                if due >= start+int(args.duration*1e9):
                    break
                wait_until_ns(due)
                row = probe.action(sequence[action_index%len(sequence)], due)
                row.update(slot=index, phase_seconds=(row['sent_ns']-start)/1e9)
                output.write(json.dumps(row)+'\n')
                output.flush()
                latest=args.run/'latest-response.json'
                pending=latest.with_suffix('.tmp')
                pending.write_text(json.dumps(dict(kind=row['kind'],latency_ms=row.get('latency_ms'),
                    sent_ns=row['sent_ns'],timeout=row['timeout'],phase_seconds=row['phase_seconds'])))
                pending.replace(latest)
                index += 1
                action_index += 1
                if row['timeout']:
                    raise TimeoutError('foreground input had no visible response for 10s')
                while start+index*500_000_000 < time.monotonic_ns()-500_000_000:
                    output.write(json.dumps(dict(kind='missed_slot', slot=index, phase_seconds=index*.5))+'\n')
                    output.flush()
                    index += 1
        result.update(success=True, actions=action_index)
        save(args.run/'measured.json', result)
        deadline = time.monotonic()+180
        while not (args.run/'finish').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('foreground finish')
            time.sleep(.05)
        g.key('ctrl','w')
        time.sleep(.3)
        g.key('ctrl','q')
        g.process.child.wait(timeout=30)
        result['clean_exit'] = True
    except Exception as exc:
        result.update(success=False, error=repr(exc))
        try:
            g.capture('failure.png')
        except Exception:
            pass
    finally:
        save(args.run/'result.json', result)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
