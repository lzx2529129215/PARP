import time
from pathlib import Path
from .config import command,sha256,save,SafetyError
from .process_manager import Process
from .window_manager import Windows
from .visual_verifier import Visual,zoom,dimensions,find_phrase
from .state_machine import wait_for


class Controller:
    def __init__(self,config,run,asset,mouse=None,windows=None,visual=None,process=None):
        self.c,self.run,self.asset=config,run,asset
        if mouse is None:
            import pyautogui
            import os
            from PIL import ImageGrab
            pyautogui.FAILSAFE=True; pyautogui.PAUSE=.12
            # Bind capture to the same X11 server as input. GNOME screenshot
            # helpers can otherwise capture the outer Wayland desktop.
            display=os.environ.get('DISPLAY')
            if not display: raise SafetyError('缺少本轮 X11 DISPLAY')
            pyautogui.screenshot=lambda:ImageGrab.grab(xdisplay=display)
            mouse=pyautogui
        self.mouse=mouse; self.windows=windows or Windows(); self.visual=visual or Visual(config)
        self.process=process or Process(run); self.wid=None; self.pid=None
        self.before_windows={w['id'] for w in self.windows.all()}
        self.before_kra=set(run.glob('*.kra'))
        self.source_kra={str(p):(p.stat().st_size,p.stat().st_mtime_ns) for p in Path(asset['path']).parent.glob('*.kra')}
        (run/'screenshots').mkdir()

    def capture(self,name):
        path=self.run/'screenshots'/name
        self.mouse.screenshot().save(path)
        if not path.is_file() or path.stat().st_size==0: raise RuntimeError('截图保存失败')
        from PIL import Image
        with Image.open(path) as saved: saved.verify()
        return str(path.relative_to(self.run))

    def wait(self,probe,kind='action'):
        return wait_for(probe,self.c['timeouts'][kind],self.c['poll_s'],self.c['stable_observations'])

    def guard(self,modal=False):
        self.process.verify(); self.windows.verify(self.wid,self.pid)
        self.windows.assert_active(self.wid,self.pid,modal)

    def region(self,name=None,modal=False):
        self.guard(modal)
        w=self.windows.active_box(self.wid,self.pid) if modal else self.windows.verify(self.wid,self.pid)
        x,y,width,height=w['x'],w['y'],w['width'],w['height']
        if name:
            a,b,c,d=self.c['regions'][name]
            x+=round(a*width); y+=round(b*height); width=round(c*width); height=round(d*height)
        image=self.mouse.screenshot()
        if x<0 or y<0 or x+width>image.width or y+height>image.height: raise RuntimeError('目标区域不在可截图桌面内')
        return image.crop((x,y,x+width,y+height)),(x,y,width,height)

    def anchor(self,labels,region=None,modal=False,click=False):
        if region=='popup':
            self.guard()
            screen=self.mouse.screenshot(); candidates=[]
            for w in self.windows.popup_boxes(self.wid,self.pid):
                box=(w['x'],w['y'],w['width'],w['height'])
                if box[0]<0 or box[1]<0 or box[0]+box[2]>screen.width or box[1]+box[3]>screen.height:
                    raise RuntimeError('菜单不在可截图桌面内')
                image=screen.crop((box[0],box[1],box[0]+box[2],box[1]+box[3]))
                try: candidates.append((self.visual.unique(image,labels),box))
                except RuntimeError: pass
            if len(candidates)!=1: raise RuntimeError('未唯一识别本轮菜单锚点：'+str(labels))
            found,box=candidates[0]
        else:
            image,box=self.region(region,modal)
            found=self.visual.unique(image,labels)
        if click:
            self.guard(modal)
            self.mouse.click(box[0]+found['x']+found['w']//2,box[1]+found['y']+found['h']//2)
        return dict(ok=True,text=found['text'],box=found)

    def key(self,*keys,modal=False):
        self.guard(modal); self.mouse.hotkey(*keys)

    def launch_krita(self):
        info=self.process.launch(); self.pid=info['pid']
        def probe():
            self.process.verify()
            try:
                w=self.windows.select(self.pid,self.before_windows)
                self.wid=w['id']; self.windows.focus(self.wid,self.pid)
                menu=self.anchor(self.c['labels']['layer_menu'],'menu')
                return dict(ok=True,window=w,process=info,main_menu=menu)
            except SafetyError:
                # Before binding or sending any document input, startup may
                # replace its splash handle. The child identity must survive.
                self.process.verify()
                return dict(ok=False,reason='startup window changed before main-menu binding')
        result=self.wait(probe,'launch'); self.windows.focus(self.wid,self.pid)
        save(self.run/'window.json',result)
        return result

    def verify_krita(self):
        def probe():
            self.guard()
            menu=self.anchor(self.c['labels']['layer_menu'],'menu')
            return dict(ok=True,process=self.process.verify(),window=self.windows.verify(self.wid,self.pid),main_menu=menu)
        return self.wait(probe,'launch')

    def open_ora(self):
        self.key('ctrl','o')
        def dialog():
            active=command(['xdotool','getactivewindow']).strip()
            if int(active)==int(self.wid,16): return dict(ok=False)
            image,_=self.region(modal=True)
            matches=find_phrase(self.visual.words(image),self.c['labels']['file_dialog'])
            return dict(ok=bool(matches),matches=matches,dialog_id=active)
        result=self.wait(dialog)
        result['dialog_screenshot']=self.capture('02_file_dialog.png')
        self.key('ctrl','l',modal=True)
        self.guard(True)
        command(['xdotool','type','--clearmodifiers','--delay','2','--',self.asset['path']],timeout=30)
        self.guard(True); self.mouse.press('enter')
        return dict(ok=True,dialog=result,submitted_path=self.asset['path'])

    def verify_document(self):
        def probe():
            self.guard(); w=self.windows.verify(self.wid,self.pid)
            status,_=self.region('status'); panel,_=self.region('layers'); canvas,_=self.region('canvas')
            text=self.visual.text(status); panel_text=self.visual.text(panel)
            size_ok=dimensions(text,self.asset['width'],self.asset['height'])
            size_channel='RGB'
            if not size_ok:
                # Krita marks high image memory with yellow status backgrounds.
                # The blue channel retains white glyph contrast on yellow.
                blue=status.convert('RGB').getchannel('B')
                alternative=self.visual.text(blue)
                if dimensions(alternative,self.asset['width'],self.asset['height']):
                    text=alternative; size_ok=True; size_channel='B'
            import PIL.ImageStat
            variance=max(PIL.ImageStat.Stat(canvas).stddev)
            try:
                self.visual.unique(panel,self.c['labels']['layer_panel'])
                panel_ok=True
            except RuntimeError:
                panel_ok=False
            ok=Path(self.asset['path']).name in w['title'] and size_ok and panel_ok and variance>3
            return dict(ok=ok,title=w['title'],status_text=text,status_ocr_channel=size_channel,panel_text=panel_text,canvas_stddev=variance)
        return self.wait(probe,'load')

    def maximize_window(self):
        self.guard(); was_maximized=self.windows.maximized(self.wid,self.pid)['ok']
        x,y=self.windows.title_point(self.wid,self.pid)
        self.mouse.doubleClick(x,y,interval=.15)
        if was_maximized:
            self.wait(lambda:dict(ok=not self.windows.maximized(self.wid,self.pid)['ok']))
            x,y=self.windows.title_point(self.wid,self.pid)
            self.mouse.doubleClick(x,y,interval=.15)
        return self.wait(lambda:self.windows.maximized(self.wid,self.pid))

    def verify_maximize(self):
        def probe():
            result=self.windows.maximized(self.wid,self.pid)
            self.anchor(self.c['labels']['layer_panel'],'layers')
            return result
        return self.wait(probe)

    def zoom_action(self,ticks):
        status,_=self.region('status'); self.zoom_before=zoom(self.visual.text(status))
        self.canvas_before,box=self.region('canvas')
        self.guard(); self.mouse.moveTo(box[0]+box[2]//2,box[1]+box[3]//2,duration=.2)
        self.mouse.scroll(ticks)
        return dict(ok=True,input_ticks=ticks,before_percent=self.zoom_before,verification_pending=True)

    def zoom_in(self): return self.zoom_action(self.c['zoom_scroll_ticks'])
    def zoom_out(self): return self.zoom_action(-self.c['zoom_scroll_ticks'])

    def verify_zoom(self,increase):
        def probe():
            status,_=self.region('status'); current=zoom(self.visual.text(status))
            canvas,_=self.region('canvas'); changed=self.visual.changed(self.canvas_before,canvas)
            direction=current>self.zoom_before if increase else current<self.zoom_before
            return dict(ok=direction and changed['changed'],before_percent=self.zoom_before,after_percent=current,canvas_change=changed)
        return self.wait(probe)

    def verify_zoom_in(self): return self.verify_zoom(True)
    def verify_zoom_out(self): return self.verify_zoom(False)

    def create_layer(self):
        self.panel_before,_=self.region('layers')
        self.old_layer_words=self.visual.words(self.panel_before)
        self.anchor(self.c['labels']['layer_menu'],'menu',click=True)
        self.wait(lambda:self.anchor(self.c['labels']['new_submenu'],'popup'))
        self.anchor(self.c['labels']['new_submenu'],'popup',click=True)
        self.wait(lambda:self.anchor(self.c['labels']['paint_layer'],'popup'))
        self.anchor(self.c['labels']['paint_layer'],'popup',click=True)
        return dict(ok=True,method='mouse Layer > New > Paint Layer',verification_pending=True)

    def verify_layer(self):
        def probe():
            panel,_=self.region('layers'); words=self.visual.words(panel)
            if not find_phrase(words,self.c['labels']['paint_layer']):
                # A thumbnail next to bold layer text can merge the first word.
                # Re-read only a selected row located from visible suffix text.
                suffixes=[label.split()[-1] for label in self.c['labels']['paint_layer']]
                focused=[]
                for partial in find_phrase(words,suffixes):
                    if not self.visual.selected(panel,partial)['selected']: continue
                    top=max(0,partial['y']-4); bottom=min(panel.height,partial['y']+partial['h']+4)
                    crop=panel.crop((0,top,panel.width,bottom))
                    for word in self.visual.words(crop,scale=3):
                        focused.append(dict(word,y=word['y']+top))
                words=focused
            old=' '.join(w['text'] for w in self.old_layer_words)
            matches=[]
            for found in find_phrase(words,self.c['labels']['paint_layer']):
                row=' '.join(w['text'] for w in words if abs(w['y']-found['y'])<max(found['h'],w['h']))
                selected=self.visual.selected(panel,found)
                if selected['selected'] and row not in old: matches.append((row,selected,found))
            if len(matches)!=1: return dict(ok=False,error='未唯一观察到被选中的新图层行')
            row,selected,found=matches[0]; changed=self.visual.changed(self.panel_before,panel)
            row_change=changed
            if not changed['changed']:
                band=(0,max(0,found['y']-4),panel.width,min(panel.height,found['y']+found['h']+4))
                row_change=self.visual.changed(self.panel_before.crop(band),panel.crop(band))
            title=self.windows.verify(self.wid,self.pid)['title']
            modified=('*' in title or 'modified' in title.lower() or '已修改' in title)
            ok=selected['selected'] and row_change['changed'] and row not in old and modified
            return dict(ok=ok,new_layer_name=row,selected=selected,panel_change=changed,new_row_change=row_change,modified=modified,
                        original_layer_count=self.asset['layers'],new_layer_visible=True,
                        total_count_method='visual added row; total not directly counted')
        return self.wait(probe)

    def close_document(self):
        self.key('ctrl','w')
        def prompt():
            image,_=self.region(modal=True); words=self.visual.words(image)
            if not all(find_phrase(words,[label]) for label in self.c['labels']['save_prompt']):
                words=self.visual.words(image,scale=3)
                if not all(find_phrase(words,[label]) for label in self.c['labels']['save_prompt']):
                    return dict(ok=False)
            return self.anchor(self.c['labels']['discard'],modal=True)
        result=self.wait(prompt,'close')
        result['prompt_screenshot']=self.capture('08_save_prompt.png')
        self.anchor(self.c['labels']['discard'],modal=True,click=True)
        return dict(ok=True,prompt=result,method='mouse discard',verification_pending=True)

    def verify_discard(self):
        def probe():
            self.guard(); w=self.windows.verify(self.wid,self.pid)
            image,_=self.region(); text=self.visual.text(image)
            empty=bool(find_phrase(self.visual.words(image),self.c['labels']['empty_workspace']))
            prompt=bool(find_phrase(self.visual.words(image),self.c['labels']['discard']))
            return dict(ok=empty and not prompt and Path(self.asset['path']).name not in w['title'],
                        title=w['title'],empty_workspace=empty,discard_prompt_visible=prompt,text=text)
        result=self.wait(probe,'close')
        actual=sha256(self.asset['path'])
        if actual!=self.asset['sha256']: raise RuntimeError('原素材摘要改变')
        if set(self.run.glob('*.kra'))!=self.before_kra: raise RuntimeError('出现意外 KRA 输出')
        source_kra={str(p):(p.stat().st_size,p.stat().st_mtime_ns) for p in Path(self.asset['path']).parent.glob('*.kra')}
        if source_kra!=self.source_kra: raise RuntimeError('素材目录出现新增或被修改的 KRA 文件')
        result.update(original_sha256_unchanged=True,no_kra_in_run_directory=True)
        return result

    def quit_krita(self):
        self.verify_discard()  # Do not quit while a document or prompt remains.
        self.key('ctrl','q')
        def probe():
            self.process.track()
            code=self.process.child.poll()
            return dict(ok=code is not None,returncode=code)
        return self.wait(probe,'exit')

    def verify_exit(self):
        result=self.process.finish()
        remaining=[w for w in self.windows.all() if w['pid']==self.pid]
        if remaining: raise RuntimeError('本轮窗口仍存在')
        if sha256(self.asset['path'])!=self.asset['sha256']: raise RuntimeError('退出后素材摘要改变')
        self.process.log.close()
        return dict(ok=True,**result,no_remaining_window=True,asset_unchanged=True)
