"""Real background Calc documents and a local Firefox image workspace."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent


class Calc:
    def __init__(self, directory, config, env):
        import uno
        self.uno = uno
        self.directory = directory
        self.log = (directory/'libreoffice.log').open('wb')
        self.pipe = 'parp_'+uuid.uuid4().hex
        self.p = subprocess.Popen(['python3','-B',str(HERE/'cg_enter.py'),str(config),'libreoffice','--',
            'libreoffice','-env:UserInstallation='+(directory/'lo-profile').as_uri(),
            '--headless','--nodefault','--nologo','--norestore',
            '--accept=pipe,name='+self.pipe+';urp;StarOffice.ServiceManager'], env=env,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
        deadline = time.monotonic()+35
        while True:
            try:
                self.context = resolver.resolve('uno:pipe,name='+self.pipe+';urp;StarOffice.ComponentContext')
                break
            except Exception:
                if self.p.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('private LibreOffice UNO startup failed')
                time.sleep(.2)
        self.desktop = self.context.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', self.context)
        hidden = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
        hidden.Name = 'Hidden'
        hidden.Value = True
        self.document = self.desktop.loadComponentFromURL('private:factory/scalc','_blank',0,(hidden,))
        self.document.enableAutomaticCalculation(False)
        self.sheet = self.document.Sheets.getByIndex(0)
        self.rows = 80000
        self.columns = 8
        for first in range(0,self.rows,2000):
            values = tuple(tuple('old-row-%06d-col-%d image-index-%06d' % (row,col,row+col)
                                 for col in range(self.columns)) for row in range(first,first+2000))
            self.sheet.getCellRangeByPosition(0,first,self.columns-1,first+1999).setDataArray(values)
        self.touch()

    def touch(self):
        values = self.sheet.getCellRangeByPosition(0,0,self.columns-1,99).getDataArray()
        return len(values)

    def grow(self):
        self.document.Sheets.insertNewByName('future',1)
        sheet = self.document.Sheets.getByName('future')
        for first in range(0,20000,2000):
            values = tuple(tuple('new-row-%06d-col-%d content' % (row,col) for col in range(8))
                           for row in range(first,first+2000))
            sheet.getCellRangeByPosition(0,first,7,first+1999).setDataArray(values)
        return 20000

    def touch_future(self):
        return len(self.document.Sheets.getByName('future').getCellRangeByPosition(0,0,7,99).getDataArray())

    def revisit(self):
        # Actual application data transfer; Python observer is outside app cgroups.
        values = self.sheet.getCellRangeByPosition(0,0,self.columns-1,self.rows-1).getDataArray()
        return len(values)

    def close(self):
        self.document.close(True)
        self.desktop.terminate()
        self.p.wait(timeout=25)
        self.log.close()


PAGE = r'''<!doctype html><meta charset="utf-8"><title>PARP image workspace</title>
<style>canvas{width:120px;height:120px}body{background:#ddd}</style><body>
<script>
const old=[], future=[]; let serial=0; const image=new Image(); image.src='/asset.png';
async function make(target,n){await image.decode(); for(let i=0;i<n;i++){
 const c=document.createElement('canvas'); c.width=4096;c.height=4096;
 const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(image,0,0);
 ctx.getImageData(0,0,1,1);document.body.append(c);target.push(c);await new Promise(r=>setTimeout(r,0));}}
async function perform(c){let result=0;
 if(c.mode==='warm'){await make(old,8);result=old.length;}
 if(c.mode==='touch'){result=old[0].toDataURL('image/png').length;}
 if(c.mode==='touch_future'){result=future[0].toDataURL('image/png').length;}
 if(c.mode==='grow'){await make(future,1);result=future.length;}
 if(c.mode==='revisit'){for(const canvas of old){result+=canvas.toDataURL('image/png').length;}}
 return result;}
async function poll(){try{const c=await(await fetch('/command',{cache:'no-store'})).json();
 if(c.serial>serial){serial=c.serial;const start=performance.now();let report={serial};
  try{report.result=await perform(c);report.elapsed_ms=performance.now()-start;}
  catch(e){report.error=String(e);}await fetch('/done',{method:'POST',body:JSON.stringify(report)});}}
 catch(e){}setTimeout(poll,30);}poll();
</script>'''


class Browser:
    def __init__(self, directory, config, env, asset):
        self.command = dict(serial=0, mode='none')
        self.replies = {}
        self.lock = threading.Lock()
        outer = self
        image = Path(asset).read_bytes()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):
                pass
            def do_GET(self):
                if self.path == '/asset.png':
                    data=image;mime='image/png'
                elif self.path.startswith('/command'):
                    with outer.lock:
                        data=json.dumps(outer.command).encode()
                    mime='application/json'
                else:
                    data=PAGE.encode();mime='text/html; charset=utf-8'
                self.send_response(200);self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
                self.end_headers();self.wfile.write(data)
            def do_POST(self):
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<8192:
                    self.send_error(400);return
                report=json.loads(self.rfile.read(length))
                with outer.lock:
                    outer.replies[report['serial']]=report
                self.send_response(204);self.end_headers()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        profile=directory/'firefox-profile';profile.mkdir()
        (profile/'user.js').write_text('user_pref("browser.aboutwelcome.enabled",false);\n'
            'user_pref("browser.startup.homepage_override.mstone","ignore");\n'
            'user_pref("browser.shell.checkDefaultBrowser",false);\n')
        executable=json.loads((HERE/'firefox-install.json').read_text())['executable']
        self.log=(directory/'firefox.log').open('wb')
        self.p=subprocess.Popen(['python3','-B',str(HERE/'cg_enter.py'),str(config),'firefox','--',executable,
            '--no-remote','--new-instance','--profile',str(profile),'http://127.0.0.1:%d/'%self.server.server_address[1]],
            env=env,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.request('warm',90)

    def request(self,mode,timeout=45):
        with self.lock:
            serial=self.command['serial']+1
            self.command=dict(serial=serial,mode=mode)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.lock:
                result=self.replies.get(serial)
            if result is not None:
                if 'error' in result:
                    raise RuntimeError('browser workload: '+result['error'])
                return result
            if self.p.poll() is not None:
                raise RuntimeError('private Firefox exited')
            time.sleep(.03)
        raise TimeoutError('browser command '+mode)

    def touch(self):return self.request('touch')
    def grow(self):return self.request('grow')
    def touch_future(self):return self.request('touch_future')
    def revisit(self):return self.request('revisit',90)
    def close(self):
        self.p.terminate()
        self.p.wait(timeout=25)
        self.server.shutdown();self.server.server_close()
        self.log.close()
