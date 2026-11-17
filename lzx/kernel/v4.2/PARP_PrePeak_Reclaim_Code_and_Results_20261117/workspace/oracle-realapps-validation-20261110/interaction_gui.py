"""External real-Krita/gedit input-to-visible-response probe on owned X11.

No application timing plugin. Screenshots/OCR are outside the timed phase.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
LAB=HERE.parent/'krita-vm-acceptance'
sys.path.insert(0,str(LAB/'userspace-peak-20261103'))
sys.path.insert(0,str(LAB.parent/'foreground_krita_gui'))
from peak_gui import LocalProcess,FastView
from oomlab_krita_gui.asset_inspector import inspect
from oomlab_krita_gui.config import load,save
from oomlab_krita_gui.gui_controller import Controller


def emit(path,**row):
    with path.open('a',buffering=1) as f:f.write(json.dumps(row)+'\n')


def open_dialog(g,path):
    g.key('ctrl','o')
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        r=subprocess.run(['xdotool','getactivewindow'],capture_output=True,text=True)
        if r.returncode or not r.stdout.strip():time.sleep(.05);continue
        if int(r.stdout.strip())!=int(g.wid,16):
            g.windows.assert_active(g.wid,g.pid,modal=True)
            g.key('ctrl','l',modal=True)
            subprocess.run(['xdotool','type','--clearmodifiers','--delay','0','--',str(path)],check=True)
            g.guard(True);g.mouse.press('enter');return
        time.sleep(.05)
    raise TimeoutError('file chooser')


def choose_visible_popup(g,label):
    # Qt can position a context menu a few pixels beyond the private screen.
    # Check ownership first, then bind only a fully visible, unique label.
    g.guard();screen=g.mouse.screenshot();matches=[]
    for box in g.windows.popup_boxes(g.wid,g.pid):
        left=max(0,box['x']);top=max(0,box['y'])
        right=min(screen.width,box['x']+box['width']);bottom=min(screen.height,box['y']+box['height'])
        if right<=left or bottom<=top:continue
        try:found=g.visual.unique(screen.crop((left,top,right,bottom)),[label])
        except RuntimeError:continue
        matches.append((left+found['x']+found['w']//2,top+found['y']+found['h']//2))
    assert len(matches)==1,'context action not uniquely visible'
    g.guard();g.mouse.click(*matches[0])


class Probe:
    def __init__(self,g,editor_wid):
        from Xlib import X,XK
        from Xlib.ext import xtest
        self.g=g;self.view=FastView(g,g.run/'actions.jsonl')
        self.d=self.view.d;self.X=X;self.XK=XK;self.xtest=xtest
        self.krita=int(g.wid,16);self.editor=int(editor_wid,16)
        self.editor_window=self.d.create_resource_object('window',self.editor)
        self.root=self.d.screen().root
        self.layer_points=[];self.layer_cursor=0
    def key(self,name):
        code=self.d.keysym_to_keycode(self.XK.string_to_keysym(name))
        assert code
        self.xtest.fake_input(self.d,self.X.KeyPress,code)
        self.xtest.fake_input(self.d,self.X.KeyRelease,code)
        self.d.flush()
    def image(self,editor=False):
        if not editor:return self.view.image()
        from PIL import Image
        raw=self.editor_window.get_image(15,55,600,220,self.X.ZPixmap,0xffffffff)
        return Image.frombytes('RGB',(600,220),raw.data,'raw','BGRX')
    def dispatch(self,kind):
        X=self.X
        if kind=='switch_editor' or kind=='switch_krita':
            from Xlib.protocol import event
            target=self.editor if kind=='switch_editor' else self.krita
            request=event.ClientMessage(window=target,client_type=self.view.active,
                data=(32,[1,X.CurrentTime,self.view.active_id(),0,0]))
            self.root.send_event(request,event_mask=X.SubstructureRedirectMask|X.SubstructureNotifyMask)
        elif kind in ('type','backspace'):
            self.key('x' if kind=='type' else 'BackSpace')
        elif kind=='layer_next':
            assert len(self.layer_points)>=4
            self.layer_cursor=(self.layer_cursor+1)%len(self.layer_points)
            x,y=self.layer_points[self.layer_cursor]
            if self.layer_cursor==0:
                self.xtest.fake_input(self.d,X.MotionNotify,x=x,y=y)
                for _ in range(3):
                    self.xtest.fake_input(self.d,X.ButtonPress,5)
                    self.xtest.fake_input(self.d,X.ButtonRelease,5)
            self.xtest.fake_input(self.d,X.MotionNotify,x=x,y=y)
            self.xtest.fake_input(self.d,X.ButtonPress,1)
            self.xtest.fake_input(self.d,X.ButtonRelease,1)
        elif kind.startswith('zoom'):
            self.xtest.fake_input(self.d,X.MotionNotify,x=400,y=500)
            button=4 if kind=='zoom_in' else 5
            self.xtest.fake_input(self.d,X.ButtonPress,button)
            self.xtest.fake_input(self.d,X.ButtonRelease,button)
        else:
            sign=1 if kind=='pan_right' else -1
            self.xtest.fake_input(self.d,X.MotionNotify,x=400,y=500)
            self.xtest.fake_input(self.d,X.ButtonPress,2)
            self.xtest.fake_input(self.d,X.MotionNotify,x=400+37*sign,y=500+23*sign)
            self.xtest.fake_input(self.d,X.ButtonRelease,2)
        self.d.flush()
    def action(self,kind,scheduled_ns):
        from PIL import ImageChops
        editor=kind in ('type','backspace','switch_krita')
        expected=self.editor if editor else self.krita
        assert self.view.active_id()==expected,'unexpected active window in private test display'
        switch=kind.startswith('switch')
        before=None if switch else self.image(editor)
        checks=[]
        begin=time.monotonic_ns()
        self.dispatch(kind)
        dispatch_done=time.monotonic_ns()
        deadline=time.monotonic()+10
        old=begin;max_gap=0;polls=0
        while time.monotonic()<deadline:
            now=time.monotonic_ns();max_gap=max(max_gap,now-old);old=now;polls+=1
            check_start=time.monotonic_ns()
            capture_done=check_start
            if switch:
                okay=self.view.active_id()==(self.editor if kind=='switch_editor' else self.krita)
            else:
                current=self.image(editor)
                capture_done=time.monotonic_ns()
                diff=ImageChops.difference(before,current).convert('L')
                changed=sum(diff.histogram()[13:])
                okay=changed>=(18 if editor else 600)
            check_done=time.monotonic_ns()
            checks.append([check_start,capture_done,check_done,bool(okay)])
            if okay:
                end=time.monotonic_ns()
                return {'kind':kind,'category':'switch' if switch else 'typing' if editor else 'krita',
                        'scheduled_ns':scheduled_ns,'sent_ns':begin,'response_ns':end,
                        'latency_ms':(end-begin)/1e6,'scheduled_response_ms':(end-scheduled_ns)/1e6,
                        'scheduler_lateness_ms':max(0,(begin-scheduled_ns)/1e6),
                        'dispatch_done_ns':dispatch_done,'observation_samples':checks,
                        'observation_step_max_ms':max((x[2]-x[0])/1e6 for x in checks),
                        'poll_gap_max_ms':max_gap/1e6,'polls':polls,'timeout':False}
            time.sleep(.02)
        end=time.monotonic_ns()
        return {'kind':kind,'category':'switch' if switch else 'typing' if editor else 'krita',
                'scheduled_ns':scheduled_ns,'sent_ns':begin,'response_ns':None,
                'timeout':True,'latency_lower_bound_ms':(end-begin)/1e6,'poll_gap_max_ms':max_gap/1e6,
                'dispatch_done_ns':dispatch_done,'observation_samples':checks,
                'observation_step_max_ms':max((x[2]-x[0])/1e6 for x in checks)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--asset',type=Path,required=True)
    p.add_argument('--template',type=Path,required=True)
    p.add_argument('--duration',type=float,default=70)
    a=p.parse_args();a.run.mkdir()
    config=load(LAB.parent/'foreground_krita_gui/configs/gui.json')
    config['timeouts']['load']=240;config['poll_s']=.2
    config['labels']['discard']=['No']
    config['labels']['save_prompt']=['Do you want to save it?','Yes','Cancel']
    asset=inspect(a.asset)
    g=Controller(config,a.run,asset,process=LocalProcess(a.run,a.template))
    result={'success':False};editor=None
    try:
        g.launch_krita();g.verify_krita()
        subprocess.run(['wmctrl','-ir',g.wid,'-b','add,maximized_vert,maximized_horz'],check=True)
        g.mouse.PAUSE=.01
        open_dialog(g,a.asset)
        view=FastView(g,a.run/'actions.jsonl');view.document(asset,'loaded')
        result['document_verified']=g.verify_document()
        # Visible canvas/title can precede completion of the small document's
        # layer dock. Wait for four owned layer rows before binding clicks.
        deadline=time.monotonic()+20;rows=[]
        while time.monotonic()<deadline:
            panel,box=g.region('layers')
            # The verified 1600x850 profile places the four labels in this
            # portion of the owned Layers region. Exclude colourful canvas,
            # thumbnails and brush presets that confuse sparse-text OCR.
            band=(round(panel.width*.475),round(panel.height*.40),
                  round(panel.width*.85),round(panel.height*.61))
            words=g.visual.words(panel.crop(band),scale=3,psm=6)
            for word in words:word['x']+=band[0];word['y']+=band[1]
            candidates=sorted([w for w in words if 'acceptance' in w['text'].lower()],key=lambda w:w['y'])
            rows=[]
            for word in candidates:
                if not rows or word['y']-rows[-1]['y']>12:rows.append(word)
            if len(rows)>=4:break
            time.sleep(.3)
        assert len(rows)>=4,'not enough verified visible layer names'
        layer_points=[(box[0]+w['x']+min(w['w'],60)//2,box[1]+w['y']+w['h']//2) for w in rows[:6]]
        g.guard();g.mouse.click(*layer_points[0],button='right')
        time.sleep(.3);g.capture('layer_context.png')
        choose_visible_popup(g,'Isolate Active Layer')
        time.sleep(.3);g.capture('isolated_layer.png')
        save(a.run/'layer-bindings.json',{'points':layer_points,'mode':'native Isolate Active Layer context action'})
        editor_home=a.run/'editor-home';editor_home.mkdir()
        gtk=editor_home/'.config/gtk-3.0';gtk.mkdir(parents=True)
        (gtk/'settings.ini').write_text('[Settings]\ngtk-cursor-blink=false\n')
        scratch=editor_home/'latency.txt';scratch.write_text('')
        env=os.environ.copy();env.update(HOME=str(editor_home),XDG_CONFIG_HOME=str(editor_home/'.config'),
            XDG_DATA_HOME=str(editor_home/'.local/share'),XDG_CACHE_HOME=str(editor_home/'.cache'),
            GSETTINGS_BACKEND='memory',GDK_BACKEND='x11')
        env.pop('WAYLAND_DISPLAY',None)
        with (a.run/'gedit.log').open('xb') as log:
            editor=subprocess.Popen(['python3',str(LAB/'cg_exec.py'),'foreground','--','gedit','--standalone','--new-window',str(scratch)],env=env,stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+30;wid=None
        while time.monotonic()<deadline:
            for line in subprocess.check_output(['wmctrl','-lpG'],text=True).splitlines():
                if int(line.split()[2])==editor.pid:wid=line.split()[0]
            if wid:break
            assert editor.poll() is None
            time.sleep(.1)
        assert wid,'gedit window not identified'
        save(a.run/'gedit-process.json',{'pid':editor.pid,'wid':wid,'scratch':str(scratch)})
        subprocess.run(['wmctrl','-ir',wid,'-b','add,maximized_vert,maximized_horz'],check=True)
        subprocess.run(['xdotool','windowactivate','--sync',g.wid],check=True)
        time.sleep(.5)
        probe=Probe(g,wid)
        probe.layer_points=layer_points
        sequence=['layer_next','layer_next','zoom_in','zoom_out','pan_left','pan_right','switch_editor','type','backspace','switch_krita']
        smoke=[]
        for kind in sequence:
            row=probe.action(kind,time.monotonic_ns());smoke.append(row)
            emit(a.run/'smoke-responses.jsonl',**row)
            if kind=='type' and not row['timeout']:probe.image(True).save(a.run/'typing-smoke.png')
            assert not row['timeout'],kind+' no visible response'
            time.sleep(.25)
        save(a.run/'smoke.json',smoke)
        g.capture('ready.png')
        save(a.run/'ready.json',{'krita_pid':g.pid,'gedit_pid':editor.pid,'krita_wid':g.wid,'gedit_wid':wid})
        deadline=time.monotonic()+180
        while not (a.run/'go').exists():
            if time.monotonic()>deadline:raise TimeoutError('go')
            time.sleep(.01)
        start=time.monotonic_ns();save(a.run/'phase-start.json',{'monotonic_ns':start,'wall_ns':time.time_ns()})
        index=0;period=500_000_000;actions=0
        while time.monotonic_ns()<start+int(a.duration*1e9):
            due=start+index*period
            if due>=start+int(a.duration*1e9):break
            while time.monotonic_ns()<due:time.sleep(max(0,min(.01,(due-time.monotonic_ns())/1e9)))
            row=probe.action(sequence[actions%len(sequence)],due)
            row.update(slot=index,phase_seconds=(row['sent_ns']-start)/1e9)
            emit(a.run/'responses.jsonl',**row);actions+=1;index+=1
            if row['timeout']:raise TimeoutError('no visible response for ten seconds')
            while start+index*period<time.monotonic_ns()-period:
                emit(a.run/'responses.jsonl',kind='missed_slot',slot=index,scheduled_ns=start+index*period,
                     phase_seconds=index*.5,reason='previous interaction or observer scheduling overran fixed input cadence')
                index+=1
        result.update(success=True,actions=actions,completed_ns=time.monotonic_ns(),phase_started_ns=start)
        save(a.run/'measured.json',result)
        deadline=time.monotonic()+180
        while not (a.run/'finish').exists():
            if time.monotonic()>deadline:raise TimeoutError('finish')
            time.sleep(.05)
        subprocess.run(['xdotool','windowactivate','--sync',wid],check=True)
        g.mouse.hotkey('ctrl','a');g.mouse.press('backspace');g.mouse.hotkey('ctrl','s')
        time.sleep(.3)
        subprocess.run(['wmctrl','-ic',wid],check=True);editor.wait(timeout=30)
        subprocess.run(['xdotool','windowactivate','--sync',g.wid],check=True)
        if '*' in view.title():g.close_document()
        else:g.key('ctrl','w')
        time.sleep(.5);g.key('ctrl','q');g.process.child.wait(timeout=60)
        result['clean_exit']=True
    except Exception as error:
        result.update(success=False,error=repr(error),scene_preserved=True)
        try:g.capture('failure.png')
        except Exception:pass
    finally:save(a.run/'result.json',result)
    return 0 if result['success'] else 1


if __name__=='__main__':raise SystemExit(main())
