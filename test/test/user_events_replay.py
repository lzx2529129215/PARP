#!/usr/bin/env python3
"""Replay mapped workbook rows on private X11 desktops, with per-row evidence.

Inputs are dispatched only to windows owned by this run's application units.
No source cell is evaluated as Python, shell, JavaScript, a URL or a file path.
"""
import argparse
from collections import Counter
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, ImageDraw, ImageGrab, ImageOps
import source20_phase1_gui as P
from user_events_plan import ROOT, build, write_plan

def command(args, **kwargs):
    return subprocess.run(list(map(str,args)), check=True, capture_output=True, text=True, timeout=20, **kwargs)

def fixtures(out):
    f=out/'fixtures';f.mkdir()
    for name in ['Desktop','testingFile','testingFile/4k','images','videos','extracted','Downloads']:
        (f/name).mkdir(parents=True,exist_ok=True)
    source=ROOT/'test/samples/wps'
    office=ROOT/'test/samples/user_events'
    for src,name in [(source/'word_0040_fixture.docx','word.docx'),(office/'briefing_large.pptx','ppt.pptx'),
                     (source/'spreadsheet_0060.xlsx','sheet.xlsx'),(source/'video_5s_test.mp4','clip.mp4')]:
        shutil.copyfile(src,f/name)
    # Operation validation uses a bounded 640x360/30 FPS clip, not the source
    # workbook's 4K/120 FPS workload. The export still must produce 60 FPS.
    command(['ffmpeg','-v','error','-y','-i',source/'video_5s_test.mp4','-vf','scale=640:360,fps=30',
             '-c:v','libx264','-preset','ultrafast','-an',f/'clip.mp4'])
    shutil.copyfile(f/'word.docx',f/'lo-word.docx')
    shutil.copyfile(office/'spreadsheet_expanded.xlsx',f/'lo-sheet.xlsx')
    for i in range(1,181):
        (f/'testingFile/4k'/f'document-{i:03d}.txt').write_text((f'Fixture document {i}\n'*120))
    for i in range(1,31):
        img=Image.new('RGB',(1600,1200),(30+i*5,55+i*3,110+i*2));d=ImageDraw.Draw(img)
        for j in range(20): d.rectangle((j*72,200+(j%4)*70,j*72+50,900),outline='white',width=5)
        d.text((100,100),f'PARP LOCAL IMAGE {i:02d}',fill='white',stroke_width=2)
        img.save(f/'images'/f'image-{i:02d}.png')
    for i in range(1,7): os.link(f/'clip.mp4',f/'videos'/f'video-{i}.mp4')
    (f/'note.txt').write_text('PARP source workbook replay\n')
    with zipfile.ZipFile(f/'source.zip','w') as z:z.writestr('payload.txt',P.PAYLOAD)
    # Local audio replaces unavailable streaming/search content.
    command(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=6','-ac','2','-y',f/'tone.wav'])
    P.write_json(out/'fixture-manifest.json',dict(substitution=True,original_size_reproduced=False,
        files={str(p.relative_to(f)):dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
               for p in f.rglob('*') if p.is_file()}))
    return f

class LocalPages:
    def __init__(self,out,f):
        self.events=[];self.pages={};self.out=out;self.f=f
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                if self.path!='/telemetry':self.send_error(404);return
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data['received_at']=time.time();owner.events.append(data)
                with (out/'web-telemetry.jsonl').open('a') as log:log.write(json.dumps(data)+'\n')
                self.send_response(204);self.end_headers()
            def do_GET(self):
                path=urlsplit(self.path).path
                if path=='/clip.mp4': body=(f/'clip.mp4').read_bytes();mime='video/mp4'
                elif path=='/image.png':body=(f/'images/image-01.png').read_bytes();mime='image/png'
                elif path.startswith('/page/'):
                    key=path.rsplit('/',1)[-1]
                    if key not in owner.pages:self.send_error(404);return
                    kind=owner.pages[key]
                    media='<video src="/clip.mp4" autoplay muted loop controls width="720"></video>' if kind=='video' else '<img src="/image.png" width="720">' if kind=='image' else ''
                    sections=''.join(f'<section><h2>Local {kind} item {i}</h2><p>Deterministic sample content for navigation and scrolling.</p>{media if kind!="video" or i==0 else ""}</section>' for i in range(16))
                    # Only generated numeric keys reach the script; workbook text is never HTML/code.
                    body=('''<!doctype html><meta charset="utf-8"><title>PARP page KEY</title>
<style>body{font:22px sans-serif;margin:35px;background:#eef2f6;color:#17283e}section{height:760px;border-bottom:2px solid #8b9bb0}video,img{max-height:480px;object-fit:contain}</style>
<h1>PARP offline operation fixture</h1>SECTIONS
<script>const id="KEY";function report(kind){fetch('/telemetry',{method:'POST',body:JSON.stringify({id,kind,y:scrollY,visible:!document.hidden,video:document.querySelector('video')?.currentTime||0})})}
addEventListener('load',()=>report('load'));addEventListener('scroll',()=>report('scroll'));
document.addEventListener('visibilitychange',()=>{report('visibility');document.querySelectorAll('video').forEach(v=>{if(document.hidden)v.pause();else v.play().catch(()=>{})})});
addEventListener('focus',()=>report('focus'));setInterval(()=>report('heartbeat'),800);</script>'''.replace('KEY',key).replace('SECTIONS',sections)).encode();mime='text/html; charset=utf-8'
                else:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def url(self,key,kind):
        self.pages[str(key)]=kind
        return f'http://127.0.0.1:{self.server.server_port}/page/{key}'
    def latest(self,key):
        latest=next((e for e in reversed(self.events) if e['id']==str(key)),None)
        return latest if latest and latest['visible'] else None
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()

class Replay(P.Replay):
    def __init__(self,out,f,pages):
        super().__init__(out);self.f=f;self.pages=pages;self.failed={};self.current_row=None
        self.tabs={};self.page={};self.doc={};self.recording=None;self.pending_save={};self.appends=0
        self.tab_ordinals={};self.home_tabs={}
        self.clipboards=[];self.directory={};self.prepared=set();self.results=[];self.extra_units=[];self.blocked_doc={}
    def record(self,app,label):
        # The outer loop captures row evidence. Avoid a screenshot per low-level key.
        return dict(active=self.state()['active_window'],label=label)
    def active(self,app):
        row=super().active(app)
        if app=='FILES' and not any('nautilus' in c.lower() for c in row['wm_classes']):
            raise RuntimeError('Owned child is not a Nautilus window: '+row['net_wm_name'])
        return row
    def input(self,app,label,*args):
        try:before=self.active(app)
        except RuntimeError:
            self.focus(app);before=self.active(app)
        result=command(['xdotool',*args]);time.sleep(.12)
        with (self.out/'inputs.jsonl').open('a') as log:
            log.write(json.dumps(dict(excel_row=self.current_row,app=app,label=label,command=list(args),
                                       window=before['window_id'],cgroup=before['cgroup'],at=time.time()))+'\n')
        return result
    def type(self,app,text):
        try:self.active(app)
        except RuntimeError:self.focus(app)
        if app=='FILES':
            self.input(app,'type-preserving-file-clipboard','type','--clearmodifiers','--delay','1',text);return
        clip=subprocess.Popen(['xclip','-selection','clipboard','-quiet'],stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        clip.stdin.write(text.encode());clip.stdin.close();self.clipboards.append(clip)
        def ready():
            probe=subprocess.run(['xclip','-o','-selection','clipboard'],capture_output=True,
                                 timeout=2)
            return probe.returncode==0 and probe.stdout==text.encode()
        P.wait_for(ready,'clipboard payload ready',3)
        self.key(app,'ctrl+v');time.sleep(.2)
    def focus(self,app):
        command(['wmctrl','-k','off'])
        try:return self.active(app)['window_id']
        except RuntimeError:pass
        windows=[r for r in self.state()['windows'] if self.owned(app,r) and r['is_normal_window']]
        if app=='FILES':windows=[r for r in windows if any('nautilus' in c.lower() for c in r['wm_classes'])]
        if not windows:raise RuntimeError('No owned normal window: '+app)
        if app=='WPS':
            contents=[r for r in windows if any(x in r['net_wm_name'] for x in ['.docx','.pptx','.xlsx'])]
            if contents:windows=contents
        wid=windows[-1]['window_id']
        for attempt in range(3):
            command(['xdotool','windowactivate','--sync',wid]);time.sleep(.2)
            try:self.active(app);return wid
            except RuntimeError:
                if attempt==2:raise
    def start(self,app,cmd):
        self.attempted.append(app);profile=self.out/app;profile.mkdir(exist_ok=True)
        alias=self.short/app.lower();alias.symlink_to(profile,target_is_directory=True)
        for name in ['config','cache','data','state','runtime']:(profile/name).mkdir(exist_ok=True,mode=0o700)
        if app=='WPS':
            folder=profile/'config/Kingsoft';folder.mkdir(parents=True,exist_ok=True)
            mode='wpsoffice\\Application%20Settings\\AppComponentMode=prome_fushion\n' if getattr(self,'wps_launch_mode','fusion')=='fusion' else ''
            (folder/'Office.conf').write_text('[6.0]\n'+mode+'common\\AcceptedEULA=true\n')
        if app=='SHOTCUT':
            folder=profile/'config/Meltytech';folder.mkdir(parents=True,exist_ok=True)
            (folder/'Shotcut.conf').write_text('[encode]\nfreeSpaceCheck=false\n')
        env={k:os.environ[k] for k in ['DISPLAY','XAUTHORITY','GDK_BACKEND','QT_QPA_PLATFORM'] if k in os.environ}
        env.update(LANG='en_US.UTF-8',LC_ALL='en_US.UTF-8',XDG_CURRENT_DESKTOP='Openbox',
                   XDG_SESSION_TYPE='x11',NO_AT_BRIDGE='1',LIBGL_ALWAYS_SOFTWARE='1',
                   QT_AUTO_SCREEN_SCALE_FACTOR='0',QT_SCALE_FACTOR='1')
        env.update({f'XDG_{key}_HOME':str(alias/name) for key,name in
                    [('CONFIG','config'),('CACHE','cache'),('DATA','data'),('STATE','state')]})
        env['XDG_RUNTIME_DIR']=str(alias/'runtime')
        if app=='WPS' and getattr(self,'wps_clean_session',False):
            env.update(QT_ACCESSIBILITY='0',QT_LINUX_ACCESSIBILITY_ALWAYS_ON='0',
                       QT_IM_MODULE='xim',GTK_MODULES='',SESSION_MANAGER='',
                       QT_QPA_PLATFORMTHEME='generic',QT_STYLE_OVERRIDE='Fusion')
        if app in {'FIREFOX','FALKON'}:
            env.update(http_proxy='',https_proxy='',all_proxy='',HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',
                       no_proxy='127.0.0.1,localhost',NO_PROXY='127.0.0.1,localhost')
        if app=='FIREFOX':env.update(WEBKIT_DISABLE_COMPOSITING_MODE='1',WEBKIT_DISABLE_DMABUF_RENDERER='1')
        if app=='FALKON':env['QTWEBENGINE_CHROMIUM_FLAGS']='--disable-smooth-scrolling'
        if app=='RHYTHMBOX' and Path('/run/user/1000/pulse/native').is_socket():
            env['PULSE_SERVER']='unix:/run/user/1000/pulse/native'
        if app=='SHOTCUT':env.update(QT_QPA_PLATFORMTHEME='generic',QT_STYLE_OVERRIDE='Fusion',XDG_CURRENT_DESKTOP='X-Generic',DESKTOP_SESSION='generic')
        # The resident PARP router preserves processes already inside their app slice.
        # Use an owned service there, so it does not migrate GUI children out of our unit.
        unit=P.gui.unit(self.out,app);slice_name=getattr(self,'service_slice',None) or 'parp-'+app.lower()+'.slice'
        launch=['systemd-run','--user','--unit='+unit,'--slice='+slice_name,
                '--property=Type=exec','--property=KillMode=control-group','--property=TimeoutStopSec=10','--collect']
        launch+=['--setenv='+k+'='+v for k,v in env.items()]
        launch+=['--','dbus-run-session',sys.executable,str(Path(__file__).resolve()),'--launch-in-bus',str(profile),*cmd]
        result=command(launch);(profile/'launcher.log').write_text(result.stdout+result.stderr)
        P.write_json(profile/'launch.json',dict(command=cmd,unit=unit,slice=slice_name,environment=env))
        with (profile/'launch-history.jsonl').open('a') as log:
            log.write(json.dumps(dict(excel_row=self.current_row,at=time.time(),command=cmd,unit=unit,environment=env))+'\n')
        P.wait_for(lambda:any(self.owned(app,r) and r['is_normal_window'] and
                             (app!='LIBREOFFICE' or 'word.docx' in r['net_wm_name'])
                             for r in self.state()['windows']),app+' owned content window',35)
        self.focus(app)
    def maximize(self,app,large=True):
        wid=self.focus(app)
        command(['wmctrl','-ir',wid,'-b',('add' if large else 'remove')+',maximized_vert,maximized_horz'])
        if not large:command(['wmctrl','-ir',wid,'-e','0,80,70,1000,720'])
        time.sleep(.25)
    def launch_command(self,app):
        f=self.f
        if app=='WPS':return self.wps_command(f/'word.docx')
        return {'FIREFOX':['epiphany','--private-instance','about:blank'],
                'FALKON':['falkon','--no-remote','about:blank'],
                'WPS':['/opt/kingsoft/wps-office/office6/wpsoffice','/prometheus',str(f/'word.docx')],
                'LIBREOFFICE':['libreoffice','-env:UserInstallation='+ (self.out/'LIBREOFFICE/lo-profile').as_uri(),
                               '--norestore','--nodefault','--nofirststartwizard',str(f/'lo-word.docx')],
                'FILES':['nautilus','--new-window',str(f/'testingFile/4k')],
                'IMAGE_VIEWER':['eog','--new-instance',str(f/'images/image-01.png')],
                'GIMP':['gimp','--no-splash','--new-instance',str(f/'images/image-01.png')],
                'VLC':['vlc','--no-one-instance','--no-video-title-show','--no-qt-privacy-ask','--no-metadata-network-access','--no-audio','--start-paused','--loop',*[str(p) for p in sorted((f/'videos').glob('*.mp4'))]],
                'FILE_ROLLER':['file-roller',str(f/'source.zip')],
                'MOUSEPAD':['mousepad','--disable-server',str(f/'note.txt')],
                'SHOTCUT':['shotcut',str(f/'clip.mp4')],
                'RHYTHMBOX':['rhythmbox',str(f/'tone.wav')],
                'KAIDAN':['kaidan'], 'CONTROL_CENTER':['gnome-control-center','info-overview'],
                'SOFTWARE':['gnome-software','--details=org.gnome.clocks.desktop']}[app]
    def wps_command(self,path):
        office=Path(getattr(self,'wps_office_dir','/opt/kingsoft/wps-office/office6'))
        if getattr(self,'wps_launch_mode','fusion')=='components':
            component={'.docx':'wps','.pptx':'wpp','.xlsx':'et'}[Path(path).suffix.lower()]
            return [str(office/component),str(path)]
        return [str(office/'wpsoffice'),'/prometheus',str(path)]
    def ensure(self,app):
        if app in self.failed:raise RuntimeError('Application prerequisite failed: '+self.failed[app])
        if not any(self.owned(app,r) and r['is_normal_window'] for r in self.state()['windows']):
            # Restart only this run's unit after explicit close. Preserve the isolated profile.
            if app in self.attempted:
                subprocess.run(['systemctl','--user','stop',P.gui.unit(self.out,app)],capture_output=True,timeout=15)
                (self.short/app.lower()).unlink(missing_ok=True)
                self.attempted.remove(app)
            try:
                self.start(app,self.launch_command(app));time.sleep(1)
                self.maximize(app)
                if app=='WPS':
                    self.doc[app]='word.docx';P.dismiss_wps_popups(self);self.editor();P.dismiss_wps_popups(self);self.editor()
                if app=='LIBREOFFICE':
                    self.key(app,'Escape');self.doc[app]='lo-word.docx'
                if app=='SHOTCUT':time.sleep(3);self.key(app,'k')
                if app=='FILES':self.directory[app]='4k'
                self.prepared.add(app)
            except Exception as exc:
                self.failed[app]=repr(exc);raise
        self.focus(app)
    def editor(self):
        app='WPS';name=self.doc.get(app,'word.docx')
        if any(self.owned(app,r) and r['net_wm_name'] in {'System Check','WPS Office'} for r in self.state()['windows']):
            P.dismiss_wps_popups(self)
        def find():
            roots={int(r['window_id'],16) for r in self.state()['windows']}
            found=subprocess.run(['xdotool','search','--onlyvisible','--name',name],capture_output=True,text=True)
            for wid in found.stdout.split():
                if int(wid) in roots:continue
                pid=subprocess.run(['xdotool','getwindowpid',wid],capture_output=True,text=True).stdout.strip()
                try:cg=Path('/proc',pid,'cgroup').read_text()
                except OSError:continue
                if not self.owned(app,{'cgroup':cg.strip()}):continue
                try:geo=command(['xdotool','getwindowgeometry','--shell',wid]).stdout
                except subprocess.CalledProcessError:continue  # Embedded window disappeared while loading.
                vals=dict(line.split('=',1) for line in geo.splitlines() if '=' in line)
                if int(vals['WIDTH'])>600 and int(vals['HEIGHT'])>350:return wid
        if getattr(self,'wps_launch_mode','fusion')=='components':
            wid=find()
            if wid:
                command(['xdotool','windowfocus',wid]);return wid
            if name=='word.docx':
                generic=next((r for r in self.state()['windows'] if self.owned(app,r)
                    and r['is_normal_window'] and r['net_wm_name'] not in {'System Check','WPS Office','wps'}
                    and 'WPS Writer' in r['net_wm_name']),None)
                if generic:
                    command(['xdotool','windowactivate','--sync',generic['window_id']]);return generic['window_id']
            row=P.wait_for(lambda:next((r for r in self.state()['windows'] if self.owned(app,r)
                and r['is_normal_window'] and name in r['net_wm_name']),None),'WPS component '+name,35)
            command(['xdotool','windowactivate','--sync',row['window_id']]);return row['window_id']
        wid=P.wait_for(find,'WPS embedded '+name,45);command(['xdotool','windowfocus',wid]);return wid
    def wps_ready(self):
        P.dismiss_wps_popups(self)
        self.editor()
        P.dismiss_wps_popups(self)
        return self.editor()
    def open_file(self,app,path):
        if app=='IMAGE_VIEWER':
            # EOG's Ctrl+O chooser did not reliably change files in this build.
            # Relaunch its native viewer with the requested local image instead.
            subprocess.run(['systemctl','--user','stop',P.gui.unit(self.out,app)],capture_output=True,timeout=15)
            (self.short/app.lower()).unlink(missing_ok=True)
            if app in self.attempted:self.attempted.remove(app)
            self.start(app,['eog','--new-instance',str(path)]);self.maximize(app);time.sleep(.5)
            return self.verify_image_header(path)
        if app=='WPS':
            # This build alternates between incompatible file panels. Reopen the
            # requested local document through WPS itself in the owned unit.
            subprocess.run(['systemctl','--user','stop',P.gui.unit(self.out,app)],capture_output=True,timeout=15)
            (self.short/app.lower()).unlink(missing_ok=True)
            if app in self.attempted:self.attempted.remove(app)
            profile=self.out/app
            archive=profile/f'previous-session-{self.current_row}';archive.mkdir()
            for name in ['config','cache','data','state','runtime']:
                if (profile/name).exists():(profile/name).rename(archive/name)
            time.sleep(.8)
            self.doc[app]=path.name
            self.start(app,self.wps_command(path))
            self.maximize(app);P.dismiss_wps_popups(self);self.editor();P.dismiss_wps_popups(self);self.editor()
            P.wait_for(lambda:any(path.name in r['net_wm_name'] for r in self.state()['windows'] if self.owned(app,r)),
                       'WPS loaded '+path.name,20)
            return dict(gui=False,lifecycle_substitution='native WPS relaunch with requested document',
                        launch_mode=getattr(self,'wps_launch_mode','fusion'),command=self.wps_command(path))
        self.key(app,'Escape');self.key(app,'ctrl+o');time.sleep(.8)
        if app in {'LIBREOFFICE','GIMP'}:
            self.key(app,'ctrl+l');time.sleep(.3);self.key(app,'ctrl+a')
        if app=='GIMP':self.input(app,'open-image-path','type','--clearmodifiers','--delay','2',str(path))
        else:self.type(app,str(path))
        self.key(app,'Return')
        time.sleep(.8)
        if app=='LIBREOFFICE':self.doc[app]=path.name;time.sleep(.8)
        elif app=='SHOTCUT':time.sleep(2);return
        P.wait_for(lambda:any(path.stem in r['net_wm_name'] for r in self.state()['windows'] if self.owned(app,r)),
                   app+' loaded '+path.name,20)
    def verify_image_header(self,path):
        header=self.out/'image-header.png'
        ImageOps.invert(ImageGrab.grab().crop((300,0,900,45)).convert('L')).resize((1800,135)).save(header)
        text=command(['tesseract',header,'stdout','--psm','7','-c','tessedit_char_whitelist=image-0123456789.png']).stdout
        if path.name not in text:raise RuntimeError('Image header mismatch: '+text)
        return dict(verification='image_header',filename=path.name,ocr=text,gui=False,lifecycle_substitution='native viewer relaunch')
    def navigate(self,folder,preserve_clipboard=False):
        app='FILES';path=self.f/('testingFile/4k' if folder=='4k' else folder)
        self.key(app,*(['ctrl+l'] if preserve_clipboard else ['Escape','ctrl+l']));self.type(app,str(path));self.key(app,'Return');time.sleep(.4)
        self.input(app,'content-focus','mousemove',850,420,'click',1)
        self.directory[app]=folder
    def screenshot(self,label):
        path=self.out/'screenshots'/f'{self.current_row:04d}-{label}.png';path.parent.mkdir(exist_ok=True)
        ImageGrab.grab().save(path);P.write_json(path.with_suffix('.json'),self.state());return str(path.relative_to(self.out))
    def select_page(self,app,page,fallback_reload=False):
        # Browsers differ in numeric shortcuts and where Ctrl+T inserts tabs.
        # Cycle real tabs and stop only when the requested page reports visibility.
        attempts=max(8,len(self.tabs.get(app,[]))*2+3)
        for attempt in range(attempts):
            proof=self.pages.latest(page)
            if proof:
                self.page[app]=page;return proof
            if attempt and attempt%4==0:self.focus(app)
            self.key(app,'ctrl+Tab');time.sleep(.3)
        if fallback_reload:
            self.focus(app)
            self.key(app,'ctrl+l');self.type(app,self.pages.url(page,'article'));self.key(app,'Return')
            proof=P.wait_for(lambda:self.pages.latest(page),'fallback browser page loaded',15)
            proof=dict(proof);proof['tab_reload_fallback']=True
            self.page[app]=page;return proof
        raise RuntimeError('Requested source page did not become visible: '+str(page))
    def web(self,event):
        app=event['target_app_key'];op=event['operation'];params=event['params'];row=event['excel_row']
        if op=='web_open':
            text=event['event1'];kind='video' if any(x in text for x in ['视频','直播','抖音']) else 'image' if any(x in text for x in ['图片','视觉','商城']) else 'article'
            if '启动' in text or '新建' in text:self.tab_ordinals[app]={}
            if params['page']=='home' and app in self.home_tabs:
                index=self.home_tabs[app]
                proof=self.select_page(app,self.tabs[app][index])
                return dict(verification='visible_tab',telemetry=proof)
            if params['new_tab']:
                self.key(app,'ctrl+t');self.tabs.setdefault(app,[]).append(row)
                import re
                ordinal=re.search(r'打开第(\d+)',text)
                if ordinal:self.tab_ordinals.setdefault(app,{})[int(ordinal[1])]=len(self.tabs[app])-1
            elif not self.tabs.get(app):self.tabs[app]=[row]
            else:self.tabs[app][self.tabs[app].index(self.page.get(app,self.tabs[app][-1]))]=row
            url=self.pages.url(row,kind)
            for attempt in range(2):
                self.key(app,'ctrl+l');self.type(app,url);self.key(app,'Return')
                try:
                    proof=P.wait_for(lambda:self.pages.latest(row),'local browser page loaded',15)
                    break
                except RuntimeError:
                    if attempt:raise
                    self.focus(app);time.sleep(.4)
            self.page[app]=row;self.input(app,'content-focus','mousemove',900,400,'click',1)
            if '首页' in text or '启动' in text or '新建' in text:self.home_tabs[app]=self.tabs[app].index(row)
            return dict(verification='page_load',telemetry=proof)
        if op=='web_tab':
            tabs=self.tabs.get(app,[]);ordinal=params['index']
            if ordinal not in self.tab_ordinals.get(app,{}):raise RuntimeError(f'No source content tab with ordinal {ordinal}')
            index=self.tab_ordinals[app][ordinal]
            proof=self.select_page(app,tabs[index],fallback_reload=True)
            return dict(verification='tab_input_and_known_page',telemetry=proof)
        key=self.page.get(app)
        if key is None:raise RuntimeError('No loaded page for scroll')
        before=self.pages.latest(key)
        last_error=None
        for attempt in range(2):
            if attempt:
                self.focus(app);time.sleep(.2)
            stamp=time.time()
            self.input(app,'wheel','mousemove',950,520,'click','--repeat',5,'--delay',35,5 if params['direction']>0 else 4)
            def changed():
                after=self.pages.latest(key)
                if after and before and after['received_at']>=stamp and (after['y']-before['y'])*params['direction']>0:return after
            try:
                proof=P.wait_for(changed,'browser scroll offset changed in requested direction',5)
                return dict(verification='scroll_offset',before=before,after=proof,retries=attempt)
            except RuntimeError as exc:
                last_error=str(exc)
                latest=self.pages.latest(key)
                if latest:before=latest
        return dict(verification='scroll_offset_unverified',before=before,after=self.pages.latest(key),fallback='wheel_input_no_verified_delta',error=last_error)
    def action(self,e):
        app,op,p=e['target_app_key'],e['operation'],e['params']
        preparation=None
        if op=='desktop':command(['wmctrl','-k','on' if p['show'] else 'off']);return dict(verification='desktop_command')
        if op=='screenshot':return dict(verification='screenshot_file',path=self.screenshot('desktop'))
        if op=='record_start':
            if self.recording:raise RuntimeError('Screen recorder already active')
            dest=self.f/f'record-{e["excel_row"]}.mkv'
            log=(self.out/'recorder.log').open('a')
            proc=subprocess.Popen(['ffmpeg','-nostdin','-y','-f','x11grab','-video_size','1280x900','-framerate','5',
                '-i',os.environ['DISPLAY'],'-c:v','libx264','-preset','ultrafast',str(dest)],stdout=log,stderr=log)
            self.recording=(proc,dest,log);time.sleep(.5)
            if proc.poll() is not None:raise RuntimeError('Screen recorder exited')
            return dict(verification='recorder_running',pid=proc.pid)
        if op=='record_stop':
            if not self.recording:raise RuntimeError('Missing start-record prerequisite')
            proc,dest,log=self.recording;proc.terminate();proc.wait(timeout=10);log.close();self.recording=None
            probe=json.loads(command(['ffprobe','-v','error','-show_format','-of','json',dest]).stdout)
            return dict(verification='recording_media',path=str(dest),media=probe)
        self.ensure(app)
        if op.startswith('web_'):return self.web(e)
        if op=='launch':return dict(verification='owned_window',cold_start_claimed=False,window=self.active(app))
        if op=='close':
            self.key(app,'alt+F4');time.sleep(.4)
            return dict(verification='close_input',remaining_windows=[r for r in self.state()['windows'] if self.owned(app,r)])
        if op=='resize':self.maximize(app,p['large'])
        elif op=='tile':
            self.maximize(app,False);command(['wmctrl','-ir',self.focus(app),'-e',f"0,{0 if p['side']=='left' else 640},30,635,840"])
        elif op=='fullscreen':self.key(app,'F11')
        elif op=='directory':self.navigate(p['directory'])
        elif op=='open_dialog':
            self.key(app,'Escape')
            if app=='WPS':self.editor()
            self.key(app,'ctrl+o')
        elif op=='document_open':
            doc=p['document'];ext={'word':'.docx','ppt':'.pptx','sheet':'.xlsx'}[doc]
            # Each online document is a distinct local file; no fabricated shared-cloud session.
            path=self.f/(f'cloud-{e["excel_row"]}-{doc}{ext}' if app=='LIBREOFFICE' else doc+ext)
            if not path.exists():shutil.copyfile(self.f/(doc+ext),path)
            detail=self.open_file(app,path) or {}
            return dict(verification='document_title',document=str(path),**detail)
        elif op=='scroll':
            if app=='FILES' and p.get('directory') and self.directory.get(app)!=p['directory']:self.navigate(p['directory'])
            if app=='LIBREOFFICE' and 'Excel' in e['event1'] and not self.doc.get(app,'').endswith('.xlsx'):
                self.open_file(app,self.f/'lo-sheet.xlsx')
                preparation=dict(document=str(self.f/'lo-sheet.xlsx'),reason='Skipped cloud upload replaced by opening a separate local spreadsheet before scrolling')
            if app=='WPS':self.editor()
            window=self.active(app)['window_id']
            geo=dict(line.split('=',1) for line in command(['xdotool','getwindowgeometry','--shell',window]).stdout.splitlines() if '=' in line)
            x=100 if app=='WPS' and p.get('pane')=='ppt' else round(int(geo['WIDTH'])*.65)
            y=round(int(geo['HEIGHT'])*.52)
            self.input(app,'wheel','mousemove','--window',window,x,y,'click','--repeat',5,'--delay',30,5 if p['direction']>0 else 4)
        elif op=='key':
            if app=='WPS' and getattr(self,'wps_launch_mode','fusion')=='components' and p['keys']==['alt+f']:
                self.wps_ready()
                self.input(app,'native-file-menu','mousemove',35,50,'click',1)
            else:self.key(app,*p['keys'])
        elif op=='zoom':self.key(app,'plus' if p['zoom_in'] else 'minus')
        elif op=='slideshow':
            self.key(app,'Escape')
            if app=='WPS':self.editor();self.input(app,'slide-outline','mousemove',100,300,'click',1)
            self.key(app,'Home','F5');time.sleep(.6)
        elif op=='bold':
            if app=='WPS':self.editor()
            if app=='WPS':self.key(app,'ctrl+Home')
            self.key(app,'ctrl+b');time.sleep(.3)
        elif op=='filter':
            self.editor();self.key(app,'ctrl+Home','ctrl+a','ctrl+shift+l','ctrl+s')
            self.screenshot('filter');return dict(verification='filter_ui_input',document=str(self.f/'sheet.xlsx'))
        elif op=='save':self.key(app,'ctrl+s')
        elif op=='save_as_dialog':
            if app=='WPS':
                self.wps_ready()
                self.key(app,'Escape')
                if getattr(self,'wps_launch_mode','fusion')!='components':
                    self.input(app,'dismiss-menu-on-canvas','mousemove',650,400,'click',1)
                self.wps_ready()
                for attempt in range(2):
                    self.key(app,'F12');time.sleep(.8)
                    try:
                        P.wait_for(lambda:self.active(app)['net_wm_name'].lower()=='wps','WPS Save As dialog',8)
                        break
                    except RuntimeError:
                        if attempt:raise
                        self.wps_ready()
            else:
                self.key(app,'Escape')
                self.key(app,'ctrl+shift+s');time.sleep(.6)
            if app=='GIMP':
                path=self.f/'edited.xcf';self.key(app,'ctrl+a');self.type(app,str(path));self.pending_save[app]=path
        elif op=='save_as_path':
            if self.active(app)['net_wm_name'].lower()!='wps':raise RuntimeError('Save As dialog is not active; refusing to type into document')
            path=self.f/f'word-saved-{e["group_index"]}.docx'
            self.input(app,'save-file-name','mousemove',660,666,'click',1);self.key(app,'ctrl+a')
            self.type(app,path.name);self.pending_save[app]=path
        elif op=='save_as_confirm':
            path=self.pending_save.get(app)
            if path is None:raise RuntimeError('Missing Save As path prerequisite')
            if app=='WPS':
                if self.active(app)['net_wm_name'].lower()!='wps':raise RuntimeError('Save As dialog is not active')
                self.input(app,'save-confirm','mousemove',1075,666,'click',1)
            else:self.key(app,'Return')
            P.wait_for(lambda:path.exists() and path.stat().st_size>0,'saved '+str(path),15)
            if app=='WPS' and not P.word_contains(path,'Perf_WPS_0040'):raise RuntimeError('Saved Word content did not match fixture')
            if app=='WPS':self.doc[app]=path.name
            if app=='GIMP' and not path.read_bytes().startswith(b'gimp xcf '):raise RuntimeError('Saved image is not XCF')
            return dict(verification='saved_file',path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        elif op=='search_files':
            self.key(app,'Escape');self.key(app,'ctrl+f');self.type(app,'document');time.sleep(.7);self.key(app,'Escape')
        elif op=='sort_files':
            # Nautilus exposes sort by name via its native view menu; use that in the pilot.
            self.key(app,'ctrl+1');self.input(app,'name-column','mousemove',220,60,'click',1)
        elif op=='copy_folder':
            self.navigate('testingFile');self.key(app,'ctrl+a');self.key(app,'ctrl+c');time.sleep(.3)
            clipboard=command(['xclip','-o','-selection','clipboard','-t','x-special/gnome-copied-files']).stdout
            if 'testingFile/4k' not in clipboard:raise RuntimeError('File clipboard did not contain source folder')
            target='Desktop/copy-'+str(e['excel_row']);(self.f/target).mkdir()
            self.navigate(target,preserve_clipboard=True);self.key(app,'ctrl+v');time.sleep(.5)
            copied=self.f/target/'4k'
            P.wait_for(lambda:copied.is_dir() and len(list(copied.iterdir()))==180,'copied folder',10)
            self.key(app,'Escape');return dict(verification='copied_file_count',count=180)
        elif op=='select_archive':self.navigate('testingFile');self.key(app,'Home')
        elif op=='archive_open':self.key(app,'Escape')
        elif op in {'archive_create','archive_extract'}:return self.archive(op,e)
        elif op=='image_open':
            path=self.f/'images'/f'image-{(p["index"]-1)%30+1:02d}.png';detail=self.open_file(app,path)
            if detail:return detail
        elif op=='image_layer':
            self.key(app,'Escape');self.key(app,'ctrl+alt+o');time.sleep(.8);self.key(app,'ctrl+l');time.sleep(.2);self.key(app,'ctrl+a')
            self.input(app,'layer-file-path','type','--clearmodifiers','--delay','2',str(self.f/'images/image-02.png'))
            self.key(app,'Return');time.sleep(.7)
            P.wait_for(lambda:'Open Image as Layers' not in self.active(app)['net_wm_name'],'image layer dialog completed',10)
        elif op in {'video_play','music_play'}:
            bus=(self.out/app/'session-bus.txt').read_text()
            service='org.mpris.MediaPlayer2.'+('vlc' if app=='VLC' else 'rhythmbox')
            base=['gdbus','call','--address',bus,'--dest',service,'--object-path','/org/mpris/MediaPlayer2']
            if app=='RHYTHMBOX':
                tone=self.f/f'tone-{e["excel_row"]}.wav';shutil.copyfile(self.f/'tone.wav',tone)
                command(base+['--method','org.mpris.MediaPlayer2.Player.OpenUri',tone.as_uri()])
            def playing():
                state=command(base+['--method','org.freedesktop.DBus.Properties.Get','org.mpris.MediaPlayer2.Player','PlaybackStatus']).stdout
                if 'Playing' in state:return state
                subprocess.run(base+['--method','org.mpris.MediaPlayer2.Player.Play'],capture_output=True,timeout=5)
                return None
            state=P.wait_for(playing,'media Playing state',10)
            return dict(verification='mpris_playing',state=state,gui=False)
        elif op=='note_paste':
            self.type(app,('\nLocal work summary: reviewed documents, images and video. Source AI response unavailable.\n'*80))
        elif op=='note_save':
            path=self.f/'saved-note.txt';self.key(app,'ctrl+shift+s');time.sleep(.4);self.key(app,'ctrl+a');self.type(app,str(path));self.key(app,'Return')
            P.wait_for(lambda:path.exists() and 'Local work summary' in path.read_text(),'saved note',10)
            return dict(verification='saved_note_content',path=str(path))
        elif op.startswith('shotcut_'):return self.shotcut(op,p,e)
        else:raise RuntimeError('Unimplemented operation: '+op)
        return dict(verification='owned_window_input_only',preparation=preparation)
    def archive(self,op,e):
        # The native application's own command interface executes compression; tracked separately from GUI input.
        app='FILE_ROLLER';profile=self.short/app.lower();dest=self.f/f'archive-{e["excel_row"]}'
        if op=='archive_create':args=['--add-to='+str(dest.with_suffix('.zip')),str(self.f/'testingFile/4k')];artifact=dest.with_suffix('.zip')
        else:args=['--extract-to='+str(dest),'--force',str(self.f/'source.zip')];artifact=dest/'payload.txt'
        # A transient child unit is explicitly recorded, not misattributed to the existing app's unit.
        unit=P.gui.unit(self.out,app).replace('.service',f'-row{e["excel_row"]}.service')
        self.extra_units.append(unit)
        env=json.loads((self.out/app/'launch.json').read_text())['environment']
        runtime=profile/f'runtime-helper-{e["excel_row"]}';runtime.mkdir(mode=0o700);env['XDG_RUNTIME_DIR']=str(runtime)
        cmd=['systemd-run','--user','--collect','--unit='+unit,'--slice='+(getattr(self,'service_slice',None) or 'parp-file_roller.slice')]
        cmd+=['--setenv='+k+'='+v for k,v in env.items()]
        cmd+=['dbus-run-session','file-roller',*args]
        command(cmd);P.wait_for(lambda:artifact.exists(),'archive artifact',60);time.sleep(.5)
        if op=='archive_create':
            with zipfile.ZipFile(artifact) as z:
                if len([n for n in z.namelist() if not n.endswith('/')])!=180:raise RuntimeError('Archive member mismatch')
        elif artifact.read_bytes()!=P.PAYLOAD:raise RuntimeError('Extracted payload mismatch')
        command(['systemctl','--user','stop',unit])
        return dict(verification='native_command_artifact',path=str(artifact),unit=unit,gui=False)
    def shotcut(self,op,p,e):
        app='SHOTCUT'
        if op=='shotcut_open':self.open_file(app,self.f/'clip.mp4');self.key(app,'k')
        elif op=='shotcut_play':self.key(app,'l');time.sleep(1);self.key(app,'k')
        elif op=='shotcut_append':
            self.key(app,'Escape','k','a');time.sleep(4)
            P.wait_for(lambda:subprocess.run(['xdotool','search','--onlyvisible','--name','^Append to Timeline$'],capture_output=True).returncode!=0,'timeline append',30)
            self.appends+=1
        elif op=='shotcut_export_panel':
            project=self.f/'four-clips.mlt';self.key(app,'ctrl+shift+s');time.sleep(.4);self.type(app,str(project));self.key(app,'Return')
            P.wait_for(lambda:project.exists(),'Shotcut project')
            entries=P.timeline_entries(project)
            if len(entries)!=4:raise RuntimeError(f'Expected four timeline clips, got {len(entries)}')
            self.key(app,'ctrl+e');return dict(verification='four_timeline_entries',entries=entries)
        elif op=='shotcut_fps':
            self.input(app,'advanced-export','mousemove',475,584,'click',1)
            self.input(app,'frames-per-second','mousemove',419,321,'click',1)
            self.key(app,'ctrl+a');self.type(app,str(p['fps']));self.key(app,'Tab');self.export_fps=p['fps']
        elif op=='shotcut_export':
            if getattr(self,'export_fps',None)!=60:raise RuntimeError('Missing 60 FPS setting prerequisite')
            dest=self.f/'four-clips-60fps.mp4'
            self.input(app,'export-file','mousemove',303,584,'click',1)
            P.wait_for(lambda:self.active(app)['net_wm_name']=='Export File','export filename')
            self.type(app,str(dest));self.key(app,'Return');observed=[]
            def exported():
                observed.extend(r for r in self.inventory(app) if any(Path(v).name in {'melt-7','melt','qmelt','ffmpeg'} for v in r['command'].split()))
                probe=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,r_frame_rate:format=duration','-of','json',str(dest)],capture_output=True,text=True)
                if probe.returncode==0:
                    media=json.loads(probe.stdout)
                    if float(media.get('format',{}).get('duration',0))>0:return media
            media=P.wait_for(exported,'complete 60 FPS export',300)
            if not any(s.get('codec_type')=='video' and s.get('r_frame_rate')=='60/1' for s in media['streams']):raise RuntimeError('Export did not have 60 FPS')
            entries=P.timeline_entries(self.f/'four-clips.mlt')
            expected_seconds=sum((P.frame_number(item['out'])-P.frame_number(item['in'])+1)/30 for item in entries)
            if len(entries)!=4 or abs(float(media['format']['duration'])-expected_seconds)>.2:
                raise RuntimeError('Four-clip export duration mismatch')
            if not observed:raise RuntimeError('No native export process observed')
            subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(dest),'-f','null','-'],check=True,capture_output=True,timeout=60)
            return dict(verification='export_media',media=media,path=str(dest),processes=observed,expected_timeline_seconds=expected_seconds)
        return dict(verification='owned_window_input_only')
    def finish(self):
        if self.recording:
            proc,_,log=self.recording;proc.terminate();proc.wait(timeout=10);log.close()
        for clip in self.clipboards:
            if clip.poll() is None:clip.terminate()
            clip.wait(timeout=5)
        for unit in self.extra_units:subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=15)
        scope=P.validate_config()
        for a in scope['apps']:
            if a['app_key'] in self.attempted:
                unit=P.gui.unit(self.out,a['app_key'])
                extras=[u for u in self.extra_units if u.startswith(unit.removesuffix('.service')+'-')]
                a.update(binding_scope_names=[unit,*extras],scope_name=unit,unit_name=unit.removesuffix('.service'))
        scope['status']='workbook_replay_run_specific_predictions_disabled'
        P.write_json(self.out/'runtime_app_scope.json',scope)
        for app in set(self.attempted):
            try:
                if self.inventory(app):self.audit(app)
                else:P.write_json(self.out/app/'process-audit.json',dict(processes=[],outside_original_unit=[],closed=True))
            except Exception as exc:P.write_json(self.out/app/'audit-error.json',dict(error=repr(exc)))
        # If an external router moved a helper, stop only PIDs with this exact
        # private profile and display. Keep evidence of the migration.
        migrated=[]
        for app in set(self.attempted):
            for proc in self.inventory(app):
                if self.owned(app,proc):continue
                pid=proc['pid']
                try:
                    fd=os.pidfd_open(pid)
                    try:
                        env=Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
                        profiles=[('XDG_CONFIG_HOME='+str(p)).encode() for p in [self.short/app.lower()/'config',self.out/app/'config']]
                        if any(p in env for p in profiles) and ('DISPLAY='+os.environ['DISPLAY']).encode() in env:
                            signal.pidfd_send_signal(fd,signal.SIGTERM);migrated.append(proc)
                    finally:os.close(fd)
                except OSError:pass
        P.write_json(self.out/'migrated-process-cleanup.json',migrated)
        if migrated:time.sleep(.5)
        return self.close()

def run_group(events,out,timing):
    out.mkdir(parents=True,exist_ok=False);f=fixtures(out);pages=LocalPages(out,f);results=[]
    with P.desktop_api.desktop('isolated',out):
        P.write_json(out/'desktop-env.json',{key:os.environ[key] for key in ['DISPLAY','XAUTHORITY','GDK_BACKEND','QT_QPA_PLATFORM','XDG_SESSION_TYPE','XDG_CURRENT_DESKTOP'] if key in os.environ})
        r=Replay(out,f,pages)
        try:
            for e in events:
                r.current_row=e['excel_row'];start=time.time()
                result=dict(excel_row=e['excel_row'],source_dataset_id=e['source_dataset_id'],app_name=e['app_name'],
                            event1=e['event1'],operation=e['operation'],target_app_key=e['target_app_key'],
                            runtime_app_id=e['runtime_app_id'],disposition=e['disposition'],reason=e['reason'])
                if timing=='original':time.sleep(e['source_delay_s'])
                if e['operation']=='skip':result.update(status='SKIPPED',verification='none')
                else:
                    before=hashlib.sha256(ImageGrab.grab().tobytes()).hexdigest()
                    try:
                        if e['target_app_key'] in r.blocked_doc and e['operation'] not in {'document_open','open_dialog','launch','close'}:
                            result.update(status='SKIPPED_PREREQUISITE',verification='none',
                                          error='Document open failed at row '+str(r.blocked_doc[e['target_app_key']]))
                            raise PrerequisiteSkip()
                        detail=r.action(e);result.update(detail,status='INPUT_SENT')
                        if e['operation']=='document_open':r.blocked_doc.pop(e['target_app_key'],None)
                        if detail['verification'] in {'page_load','scroll_offset','document_title','saved_file','saved_note_content',
                                                      'copied_file_count','native_command_artifact','screenshot_file','recording_media','four_timeline_entries','export_media','mpris_playing','image_header'}:
                            result['status']='VERIFIED'
                        after=hashlib.sha256(ImageGrab.grab().tobytes()).hexdigest()
                        result['screen_changed']=before!=after
                        if e['operation'] not in {'web_scroll','web_open'} or e['excel_row']%30==0:
                            result['screenshot']=r.screenshot('after')
                    except PrerequisiteSkip:pass
                    except Exception as exc:
                        if e['operation']=='document_open':r.blocked_doc[e['target_app_key']]=e['excel_row']
                        result.update(status='FAILED',error=repr(exc),screenshot=r.screenshot('failed'))
                        print(f"  row {e['excel_row']} {e['target_app_key']} FAILED {exc}",flush=True)
                result.update(started_at=start,completed_at=time.time(),source_delay_s=e['source_delay_s'])
                results.append(result)
                with (out/'results.jsonl').open('a') as log:log.write(json.dumps(result,ensure_ascii=False)+'\n')
                P.write_json(out/'progress.json',dict(last_excel_row=e['excel_row'],counts=dict(Counter(x['status'] for x in results))))
                if len(results)%40==0:print(f"  {len(results)}/{len(events)} rows: {dict(Counter(x['status'] for x in results))}",flush=True)
        finally:
            cleanup=r.finish();pages.close()
            P.write_json(out/'summary.json',dict(rows=len(results),counts=dict(Counter(x['status'] for x in results)),cleanup=cleanup,
                       original_timing=timing=='original',prediction_tested=False,performance_tested=False))
    return results

class PrerequisiteSkip(Exception):
    """A retained source boundary whose required document did not load."""

def main():
    if '--continuous' in sys.argv:
        from user_events_oracle import main as continuous_main
        return continuous_main([arg for arg in sys.argv[1:] if arg!='--continuous'])
    p=argparse.ArgumentParser();p.add_argument('--xlsx',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--groups',nargs='*',type=int);p.add_argument('--rows',nargs='*',type=int)
    p.add_argument('--resume',action='store_true',help='Reuse completed groups with the same source hash and row sequence')
    p.add_argument('--retry-failed',action='store_true',help='With --resume, archive and rerun complete groups containing failures')
    p.add_argument('--timing',choices=['compressed','original'],default='compressed');a=p.parse_args()
    plan=build(a.xlsx);out=a.output_dir.resolve()
    if a.resume:
        old=json.loads((out/'mapping/plan.json').read_text())
        if old['source_sha256']!=plan['source_sha256'] or old['events']!=plan['events']:
            raise ValueError('Resume requires unchanged source data and operation mapping')
    else:out.mkdir(parents=True,exist_ok=False);write_plan(plan,out/'mapping')
    with (out/'execution-attempts.jsonl').open('a') as log:
        log.write(json.dumps(dict(at=time.time(),resume=a.resume,retry_failed=a.retry_failed,
            source_sha256=plan['source_sha256'],code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))+'\n')
    all_results=[]
    for group in plan['groups']:
        if a.groups and group['index'] not in a.groups:continue
        es=[e for e in plan['events'] if e['group_index']==group['index'] and (not a.rows or e['excel_row'] in a.rows)]
        if not es:continue
        group_out=out/f'group-{group["index"]:02d}'
        previous=[]
        if a.resume and (group_out/'summary.json').exists() and (group_out/'results.jsonl').exists():
            previous=[json.loads(line) for line in (group_out/'results.jsonl').read_text().splitlines()]
        complete=[r['excel_row'] for r in previous]==[e['excel_row'] for e in es]
        retry=a.retry_failed and any(r['status'] in {'FAILED','SKIPPED_PREREQUISITE'} for r in previous)
        if complete and not retry:
            print(f"Reusing complete group {group['index']}",flush=True);all_results+=previous
        else:
            if group_out.exists():
                if not a.resume:raise FileExistsError(group_out)
                attempts=out/'attempts';attempts.mkdir(exist_ok=True)
                group_out.rename(attempts/(group_out.name+'-'+str(time.time_ns())))
            print(f"Group {group['index']}: {len(es)} rows",flush=True)
            all_results+=run_group(es,group_out,a.timing)
        P.write_json(out/'summary.json',dict(source_sha256=plan['source_sha256'],planned_rows=len(plan['events']),
                   processed_rows=len(all_results),counts=dict(Counter(e['status'] for e in all_results)),
                   complete_traversal=len(all_results)==len(plan['events']),all_operations_verified=all(e['status']=='VERIFIED' for e in all_results),
                   prediction_tested=False,performance_tested=False))
        write_report(plan,all_results,out)
    return 0

def write_report(plan,results,out):
    fields=['excel_row','source_dataset_id','app_name','event1','target_app_key','runtime_app_id',
            'operation','disposition','status','verification','reason','error','source_delay_s','started_at','completed_at']
    with (out/'execution.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(results)
    counts=Counter(r['status'] for r in results)
    lines=['# user_events 第一阶段回放结果','',f"源记录 {len(plan['events'])} 条；本次遍历 {len(results)} 条。",'',
           f'结果分类：`{dict(counts)}`。','',
           '`VERIFIED`：有页面反馈、文件内容或媒体状态等后置校验；`INPUT_SENT`：已向所属窗口发送输入，未逐项验证业务结果。',
           '`SKIPPED`：映射时明确跳过；`FAILED`：尝试失败；`SKIPPED_PREREQUISITE`：前置文档未打开，保留该条不误操作别的文档。','',
           '| 原应用 | 遍历 | 有后置校验 | 仅输入 | 跳过 | 失败/前置失败 |',
           '|---|---:|---:|---:|---:|---:|']
    for app in dict.fromkeys(e['app_name'] for e in plan['events']):
        es=[r for r in results if r['app_name']==app];c=Counter(r['status'] for r in es)
        if es:lines.append(f"| {app} | {len(es)} | {c['VERIFIED']} | {c['INPUT_SENT']} | {c['SKIPPED']} | {c['FAILED']+c['SKIPPED_PREREQUISITE']} |")
    lines+=['','12 组分别使用独立桌面和素材副本，组内保持源顺序。默认压缩等待，启动步骤允许复用已启动应用，不声称原始冷启动条件。',
            '网页/图片/视频/文档使用有限本地素材，不能据此宣称原始内容、在线服务、文件大小或内存压力等价。',
            '压缩/解压、图片重新打开和媒体播放可使用应用原生命令/MPRIS；日志以 gui=false 明确标识。',
            'WPS 文档打开可通过原生命令重启本次专用实例，以新配置加载指定文件；不保留之前的文档标签，具体步骤见逐行生命周期记录。',
            '截图和录屏仅针对本次独立桌面。此执行器未调用模型训练、预测或内核回收写入接口；常驻 PARP 服务可能观察这些应用进程。',
            '', '逐行结果见 execution.csv；每组 results.jsonl 含具体证据，screenshots/ 含界面和窗口归属，fixtures/ 保留输出文件。',
            '每组 runtime_app_scope.json 为本次实际运行单元映射。每个应用的 process-audit.json 和 cleanup.json 记录归属和清理。']
    errors=[r for r in results if r['status']=='FAILED']
    if errors:
        lines+=['','实际失败：']+[f"- Excel 第 {r['excel_row']} 行，{r['app_name']}：{r.get('error','')}" for r in errors]
    (out/'report.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    if len(sys.argv)>2 and sys.argv[1]=='--launch-in-bus':
        (Path(sys.argv[2])/'session-bus.txt').write_text(os.environ['DBUS_SESSION_BUS_ADDRESS'])
        os.execvpe(sys.argv[3],sys.argv[3:],os.environ)
    sys.exit(main())
