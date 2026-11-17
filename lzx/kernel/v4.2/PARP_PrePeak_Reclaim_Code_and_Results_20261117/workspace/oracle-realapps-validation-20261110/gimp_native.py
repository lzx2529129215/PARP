#!/usr/bin/env python3
"""Native GIMP PDB workload. Warm history is an explicit transfer-test fixture."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import zipfile

HERE=Path(__file__).resolve().parent
LAB=HERE.parent/'krita-vm-acceptance'

class Gimp:
    def __init__(self,directory,env):
        self.directory=directory
        self.asset_root=Path(env['ORACLE_APP_ASSETS'])
        self.log=(directory/'gimp.log').open('xb',buffering=0)
        self.data=b''; self.serial=0
        private=directory/'gimp-private'; private.mkdir()
        (private/'gimprc').write_text('(tile-cache-size 4096M)\n(num-processors 1)\n(undo-levels 0)\n')
        env=env.copy(); env['GIMP2_DIRECTORY']=str(private)
        self.p=subprocess.Popen(['python3',str(LAB/'cg_exec.py'),'background','--',
            'gimp','--new-instance','--no-interface','--no-splash','--no-data','--no-fonts',
            '--console-messages','--batch-interpreter=plug-in-script-fu-eval','--batch','-'],
            env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
        self.selector=selectors.DefaultSelector(); self.selector.register(self.p.stdout,selectors.EVENT_READ)
        self.command('(gimp-message "native acceptance instance")')

    def command(self,scheme,timeout=120):
        self.serial+=1
        marker=f'ACCEPTANCE_DONE_{self.serial:04d}'
        self.p.stdin.write(f'(begin {scheme} (gimp-message "{marker}"))\n'.encode())
        self.p.stdin.flush()
        end=time.monotonic()+timeout; begin=len(self.data)
        while time.monotonic()<end:
            for key,_ in self.selector.select(.2):
                block=os.read(key.fd,65536)
                if not block: raise RuntimeError('native GIMP output ended')
                self.log.write(block); self.data+=block
            if marker.encode() in self.data[begin:]: return
            if self.p.poll() is not None: raise RuntimeError('native GIMP exited unexpectedly')
        raise TimeoutError(f'GIMP command {self.serial} timed out; see gimp.log')

    def rss(self):
        # Main application RSS; helper/plugin RSS is separately retained in samples.
        fields=dict(line.split(':',1) for line in Path(f'/proc/{self.p.pid}/status').read_text().splitlines() if ':' in line)
        return int(fields['VmRSS'].split()[0])*1024

    def prepare(self,warm_layers=12):
        assets=self.directory/'gimp-png'; assets.mkdir()
        with zipfile.ZipFile(self.asset_root/'formal_4096_48layers.ora') as archive:
            names=sorted(n for n in archive.namelist() if n.startswith('data/') and n.endswith('.png'))
            for index,name in enumerate(names[:max(4,warm_layers)]):
                (assets/f'layer-{index:02d}.png').write_bytes(archive.read(name))
        self.assets=assets
        if warm_layers:
            source=json.dumps(str(assets/'layer-00.png'))
            self.command(f'(define old-image (car (gimp-file-load RUN-NONINTERACTIVE {source} {source}))) (gimp-image-undo-disable old-image)')
            for index in range(1,warm_layers):
                source=json.dumps(str(assets/f'layer-{index:02d}.png'))
                self.command(f'(let ((layer (car (gimp-file-load-layer RUN-NONINTERACTIVE old-image {source})))) (gimp-image-insert-layer old-image layer 0 0))')
        self.command('(define old-layers (cadr (gimp-image-get-layers old-image))) (if (not (= 16 (car (gimp-image-get-layers old-image)))) (error "unexpected old layer count"))')

    def touch(self,image,first,count):
        self.command(f'(let* ((layers (cadr (gimp-image-get-layers {image})))) (let loop ((i {first})) (if (< i {first+count}) (begin (gimp-histogram (aref layers i) HISTOGRAM-VALUE 0 255) (loop (+ i 1))))))')

    def controlled_stage(self,scenario,index):
        if scenario=='new_pages' and index>0:
            if index==1:
                source=json.dumps(str(self.assets/'layer-00.png'))
                self.command(f'(define future-image (car (gimp-file-load RUN-NONINTERACTIVE {source} {source}))) (gimp-image-undo-disable future-image)')
                for i in range(1,8):
                    source=json.dumps(str(self.assets/f'layer-{i:02d}.png'))
                    self.command(f'(let ((layer (car (gimp-file-load-layer RUN-NONINTERACTIVE future-image {source})))) (gimp-image-insert-layer future-image layer 0 0))')
            self.touch('future-image',0,8)
        else:
            self.touch('old-image',index*3 if scenario=='rotate_old' else 0,3)

    def stage(self,index):
        if index==0:
            source=json.dumps(str(self.assets/'layer-00.png'))
            self.command(f'(define next-image (car (gimp-file-load RUN-NONINTERACTIVE {source} {source})))')
        elif index==1:
            for number in (1,2,3):
                source=json.dumps(str(self.assets/f'layer-{number:02d}.png'))
                self.command(f'(let ((layer (car (gimp-file-load-layer RUN-NONINTERACTIVE next-image {source})))) (gimp-image-insert-layer next-image layer 0 0))')
        elif index==2:
            self.command('(gimp-image-flip next-image ORIENTATION-HORIZONTAL)')
        elif index==3:
            self.command('(gimp-desaturate-full (car (gimp-image-get-active-layer next-image)) DESATURATE-LUMINOSITY)')
        else: raise ValueError('four stages only')

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.write(b'(gimp-quit 0)\n'); self.p.stdin.flush(); self.p.stdin.close()
            self.p.wait(timeout=30)
        self.log.close(); self.selector.close()
        if self.p.returncode!=0: raise RuntimeError(f'GIMP exit {self.p.returncode}')
