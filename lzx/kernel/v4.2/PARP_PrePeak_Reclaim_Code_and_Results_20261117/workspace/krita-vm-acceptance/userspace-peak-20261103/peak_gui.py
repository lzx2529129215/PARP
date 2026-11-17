"""External GUI driver; no policy imports, plugins, or simulated app allocations.

The small document is useful work before the designated large-document peak.
All input is confined to the separately created owned X11 test desktop.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
sys.path.insert(0, str(LAB.parent/'foreground_krita_gui'))
from oomlab_krita_gui.asset_inspector import inspect
from oomlab_krita_gui.config import load, save
from oomlab_krita_gui.gui_controller import Controller
from oomlab_krita_gui.process_manager import Process, identity, qt_environment


def emit(path, event, **values):
    row = dict(event=event, monotonic_ns=time.monotonic_ns(), wall_ns=time.time_ns(), **values)
    with path.open('a', encoding='utf-8') as out:
        out.write(json.dumps(row)+'\n')
    return row


class LocalProcess(Process):
    def __init__(self, run, template):
        super().__init__(run)
        self.template = template

    def launch(self):
        env, audit = qt_environment()
        for key, name in [('HOME', 'private-home'), ('XDG_CONFIG_HOME', 'private-config'),
                          ('XDG_DATA_HOME', 'private-data'), ('XDG_CACHE_HOME', 'private-cache')]:
            destination = self.run/name
            source = self.template/name
            if source.exists():
                shutil.copytree(source, destination)
            else:
                destination.mkdir(mode=0o700)
            env[key] = str(destination)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'OOM_KRITA_RUN'):
            env.pop(key, None)
        self.log = (self.run/'krita.log').open('xb')
        self.child = subprocess.Popen(['python3', str(LAB/'cg_exec.py'), 'foreground', '--', 'krita'],
            env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            self.info = identity(self.child.pid)
            if Path(self.info['exe']).name == 'krita':
                break
            if self.child.poll() is not None:
                raise RuntimeError('Krita launch failed')
            time.sleep(.02)
        else:
            raise TimeoutError('native Krita exec')
        save(self.run/'process.json', self.info)
        save(self.run/'qt-environment.json', audit)
        return self.info


class FastView:
    def __init__(self, g, timeline):
        from Xlib import display, X
        self.d = display.Display(os.environ['DISPLAY'])
        self.X = X
        self.window = self.d.create_resource_object('window', int(g.wid, 16))
        self.root = self.d.screen().root
        self.name = self.d.intern_atom('_NET_WM_NAME')
        self.active = self.d.intern_atom('_NET_ACTIVE_WINDOW')
        self.g = g
        self.timeline = timeline

    def title(self):
        prop = self.window.get_full_property(self.name, self.X.AnyPropertyType)
        if not prop:
            return ''
        return prop.value if isinstance(prop.value, str) else bytes(prop.value).decode('utf-8', errors='replace')

    def active_id(self):
        prop = self.root.get_full_property(self.active, self.X.AnyPropertyType)
        return int(prop.value[0]) if prop else 0

    def image(self, kind='canvas'):
        from PIL import Image
        shape = self.window.get_geometry()
        if kind == 'canvas':
            x, y, w, h = shape.width//3, shape.height//3, 240, 160
        elif kind == 'status':
            x, y, w, h = 0, shape.height-80, shape.width, 80
        else:
            x, y, w, h = shape.width-350, 100, 345, min(550, shape.height-120)
        raw = self.window.get_image(x, y, w, h, self.X.ZPixmap, 0xffffffff)
        if len(raw.data) != w*h*4:
            raise RuntimeError('unsupported X11 pixel layout')
        return Image.frombytes('RGB', (w, h), raw.data, 'raw', 'BGRX')

    def document(self, asset, label, timeout=240):
        from PIL import ImageStat
        deadline = time.monotonic()+timeout
        stable, old, first = 0, None, None
        previous_poll_ns = time.monotonic_ns()
        max_poll_gap = 0
        read_ns = []
        while time.monotonic() < deadline:
            begin = time.monotonic_ns()
            max_poll_gap = max(max_poll_gap, begin-previous_poll_ns)
            previous_poll_ns = begin
            if self.g.process.child.poll() is not None:
                raise RuntimeError('Krita exited during open')
            title = self.title()
            okay = Path(asset['path']).name in title and self.active_id() == int(self.g.wid, 16)
            if okay:
                canvas = self.image()
                checksum = hashlib.sha256(canvas.tobytes()).hexdigest()
                nonblank = max(ImageStat.Stat(canvas).stddev) > 3
                stable = stable+1 if nonblank and checksum == old else 0
                old = checksum
                if first is None and nonblank:
                    first = begin
                if stable >= 2:
                    canvas.save(self.g.run/(label+'-canvas.png'))
                    self.image('status').save(self.g.run/(label+'-status.png'))
                    self.image('layers').save(self.g.run/(label+'-layers.png'))
                    return emit(self.timeline, label+'_visible_ready', first_candidate_ns=first,
                        max_poll_gap_ns=max_poll_gap, poll_read_ns=read_ns[-30:], title=title,
                        criterion='matching title, active own main window, nonblank stable canvas over two poll intervals',
                        limitation='visible completion proxy; subsequently validate document and first zoom response')
            else:
                stable, old, first = 0, None, None
            read_ns.append(time.monotonic_ns()-begin)
            time.sleep(.1)
        raise TimeoutError(label+' open not visibly complete')


def prepare_dialog(g, path):
    g.key('ctrl', 'o')
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        g.process.verify()
        active = subprocess.check_output(['xdotool', 'getactivewindow'], text=True).strip()
        if int(active) != int(g.wid, 16):
            g.windows.assert_active(g.wid, g.pid, modal=True)
            title = subprocess.check_output(['xdotool', 'getwindowname', active], text=True).strip()
            if 'open' not in title.lower():
                raise RuntimeError('unexpected own dialog: '+title)
            g.key('ctrl', 'l', modal=True)
            subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '0', '--', str(path)], check=True)
            return
        time.sleep(.05)
    raise TimeoutError('open file dialog')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--asset', type=Path, required=True)
    p.add_argument('--small', type=Path, required=True)
    p.add_argument('--template', type=Path, required=True)
    a = p.parse_args()
    a.run.mkdir()
    timeline = a.run/'actions.jsonl'
    config = load(LAB.parent/'foreground_krita_gui/configs/gui.json')
    config['labels']['discard'] = ['No']
    config['labels']['save_prompt'] = ['Do you want to save it?', 'Yes', 'Cancel']
    config['poll_s'] = .2
    config['timeouts']['load'] = 240
    large, small = inspect(a.asset), inspect(a.small)
    g = Controller(config, a.run, small, process=LocalProcess(a.run, a.template))
    result = {'success': False, 'measurement': 'visible completion plus subsequent UI verification'}
    try:
        g.launch_krita()
        g.verify_krita()
        subprocess.run(['wmctrl', '-ir', g.wid, '-b', 'add,maximized_vert,maximized_horz'], check=True)
        g.mouse.PAUSE = .01
        prepare_dialog(g, a.small)
        view = FastView(g, timeline)
        save(a.run/'ready.json', dict(pid=g.pid, wid=g.wid, asset=large, small=small))
        deadline = time.monotonic()+180
        while not (a.run/'go').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('experiment go')
            time.sleep(.01)
        g.guard(True)
        start = emit(timeline, 'small_open_submit')
        g.mouse.press('enter')
        small_ready = view.document(small, 'small')
        emit(timeline, 'peak_dialog_start')
        prepare_dialog(g, a.asset)
        g.guard(True)
        peak = emit(timeline, 'peak_open_submit')
        save(a.run/'peak-start.json', peak)
        g.mouse.press('enter')
        ready = view.document(large, 'peak')
        # Useful input follows visible completion immediately, without OCR.
        g.asset = large
        before = view.image()
        g.guard()
        g.mouse.moveTo(g.windows.verify(g.wid, g.pid)['x']+500,
                       g.windows.verify(g.wid, g.pid)['y']+350)
        zoom = emit(timeline, 'zoom_submit')
        g.mouse.scroll(2)
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if view.image().tobytes() != before.tobytes():
                break
            time.sleep(.05)
        else:
            raise TimeoutError('zoom has no visible response')
        zoom_end = emit(timeline, 'zoom_visible_change')
        before_layer = view.image('layers').tobytes()
        g.guard()
        layer = emit(timeline, 'layer_submit')
        g.mouse.press('insert')
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if '*' in view.title() and view.image('layers').tobytes() != before_layer:
                break
            time.sleep(.05)
        else:
            raise TimeoutError('new paint layer not observed')
        layer_end = emit(timeline, 'layer_visible_change')
        result.update(peak_visible_response_seconds=(ready['monotonic_ns']-peak['monotonic_ns'])/1e9,
            peak_to_first_zoom_response_seconds=(zoom_end['monotonic_ns']-peak['monotonic_ns'])/1e9,
            zoom_visible_change_seconds=(zoom_end['monotonic_ns']-zoom['monotonic_ns'])/1e9,
            layer_visible_change_seconds=(layer_end['monotonic_ns']-layer['monotonic_ns'])/1e9,
            small_open_seconds=(small_ready['monotonic_ns']-start['monotonic_ns'])/1e9)
        # Revisit old background data before expensive OCR, so OCR does not
        # introduce a long idle interval or compete for CPU in that measurement.
        save(a.run/'actions-complete.json', result)
        deadline = time.monotonic()+180
        while not (a.run/'verify-go').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('background revisit completion')
            time.sleep(.01)
        check_start = emit(timeline, 'post_timing_verification_start')
        g.capture('peak_after_actions.png')
        verified = g.verify_document()
        check_end = emit(timeline, 'post_timing_verification_end')
        result.update(success=True, document_verified=verified,
            post_timing_verification_seconds=(check_end['monotonic_ns']-check_start['monotonic_ns'])/1e9)
        save(a.run/'measured.json', result)
        deadline = time.monotonic()+180
        while not (a.run/'finish').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('post-measurement finish')
            time.sleep(.05)
        g.close_document()
        g.asset = small
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if Path(small['path']).name in view.title() and view.active_id() == int(g.wid, 16):
                break
            time.sleep(.05)
        else:
            raise TimeoutError('large document close not complete')
        # Small document has no edits, so Ctrl+W closes without a save question.
        g.key('ctrl', 'w')
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if Path(small['path']).name not in view.title() and '*' not in view.title() and view.active_id() == int(g.wid, 16):
                break
            time.sleep(.05)
        else:
            raise TimeoutError('small unmodified document did not close')
        g.capture('empty_workspace_before_quit.png')
        g.key('ctrl', 'q')
        g.process.child.wait(timeout=60)
        g.verify_exit()
        if not a.template.exists():
            a.template.mkdir()
            for name in ('private-home', 'private-config', 'private-data', 'private-cache'):
                shutil.copytree(a.run/name, a.template/name)
            result['initialized_profile_template_created'] = True
        result['clean_exit'] = True
    except Exception as error:
        result.update(success=False, error=repr(error), scene_preserved=True)
        try:
            g.capture('failure.png')
        except Exception:
            pass
        emit(timeline, 'gui_failed', error=repr(error))
    finally:
        save(a.run/'result.json', result)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
