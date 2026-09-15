#!/usr/bin/env python3
"""Replay four source20 phase-one apps on the existing private LSApp desktop.

This is GUI/content acceptance, not model training or kernel deployment.
Every run requires a new output directory; source fixtures are never edited.
"""
import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

import lsapp_30_gui as gui
import visit_window_scenarios as desktop_api

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'test/configs/source20_phase1'
APPS = ('WPS', 'FILE_ROLLER', 'MOUSEPAD', 'SHOTCUT')
TOKEN = 'PARP_PHASE1_GUI_EDIT_20260912'
PAYLOAD = b'PARP archive GUI extraction verification\n'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def wait_for(predicate, description, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ET.ParseError, zipfile.BadZipFile):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Timed out: ' + description)
        time.sleep(.2)


def validate_config():
    scope = json.loads((CONFIG / 'runtime_app_scope.json').read_text())
    vocab = json.loads((CONFIG / 'app_vocab_duration.json').read_text())
    apps = scope['apps']
    assert len(apps) == 30
    assert {a['app_id'] for a in apps} == set(range(1, 31))
    assert len({a['app_key'] for a in apps}) == 30
    assert set(APPS) <= {a['app_key'] for a in apps}
    assert vocab == dict([(a['vocab_name'], a['app_id'] - 1) for a in apps] +
                         [('<PAD>', 30), ('<UNKNOWN>', 31)])
    assert not any(a['prediction_enabled'] for a in apps)
    # Exercise the real runtime parser, not only the JSON representation.
    sys.path.insert(0, str(ROOT / 'lzx/service/runtime_monitor'))
    from core.runtime_scope import load_runtime_app_scope
    parsed = load_runtime_app_scope(CONFIG / 'runtime_app_scope.json')
    assert set(APPS) <= set(parsed.target_apps)
    return scope


class Replay:
    def __init__(self, out):
        self.out = out
        self.serial = 0
        self.short = Path(tempfile.mkdtemp(prefix='p1-'))
        self.attempted = []
        self.commands = {}
        gui.V = desktop_api

    def state(self):
        reader = desktop_api._X11PropertyReader()
        try:
            rows = []
            for wid in reader.client_window_ids():
                prop = reader.window_properties(wid)
                if prop:
                    row = dataclasses.asdict(prop)
                    try:
                        row['cgroup'] = Path(f'/proc/{prop.pid}/cgroup').read_text().strip()
                    except OSError:
                        row['cgroup'] = ''
                    rows.append(row)
            return dict(active_window=reader.active_window_id(), windows=rows)
        finally:
            reader.close()

    def owned(self, app, row):
        return gui.unit(self.out, app) in row['cgroup'].split('/')

    def active(self, app):
        state = self.state()
        row = next((r for r in state['windows'] if r['window_id'] == state['active_window']), None)
        if row is None or not self.owned(app, row):
            raise RuntimeError('Active window is not owned by ' + app)
        return row

    def record(self, app, label):
        self.serial += 1
        return gui.snapshot(self.out, f'{self.serial:03d}-{app}-{label}')

    def input(self, app, label, *command):
        before = self.active(app)
        started = time.time()
        subprocess.run(['xdotool', *map(str, command)], check=True, timeout=10)
        time.sleep(.5)
        after = self.record(app, label)
        with (self.out / app / 'actions.jsonl').open('a') as f:
            f.write(json.dumps(dict(started=started, completed=time.time(), label=label,
                                    command=list(command), before=before, after=after)) + '\n')

    def key(self, app, *keys):
        self.input(app, '-'.join(keys), 'key', '--clearmodifiers', *keys)

    def type(self, app, text):
        if app == 'WPS':
            # WPS duplicates/drops rapid synthetic key events. Its existing
            # automation uses clipboard paste; this clipboard is private to
            # the owned X server, not the user's desktop.
            self.active(app)
            clip = subprocess.Popen(['xclip', '-selection', 'clipboard', '-quiet'],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                clip.stdin.write(text.encode())
                clip.stdin.close()
                time.sleep(.2)
                self.key(app, 'ctrl+v')
                with (self.out / app / 'actions.jsonl').open('a') as f:
                    f.write(json.dumps(dict(label='clipboard-payload', text=text,
                                            sha256=hashlib.sha256(text.encode()).hexdigest())) + '\n')
            finally:
                clip.terminate()
                clip.wait(timeout=5)
        else:
            self.input(app, 'type', 'type', '--clearmodifiers', '--delay', '20', text)

    def focus(self, app):
        windows = [r for r in self.state()['windows'] if self.owned(app, r) and r['is_normal_window']]
        if app == 'WPS':
            windows = [r for r in windows if 'phase1.docx' in r['net_wm_name']]
        if app == 'SHOTCUT':
            windows = [r for r in windows if r['net_wm_name'].endswith(' - Shotcut')]
        if not windows:
            raise RuntimeError('No content window: ' + app)
        wid = windows[-1]['window_id']
        subprocess.run(['xdotool', 'windowactivate', '--sync', wid], check=True, timeout=10)
        self.active(app)
        self.record(app, 'focus-return')
        return wid

    def start(self, app, command):
        self.attempted.append(app)
        profile = self.out / app
        profile.mkdir(exist_ok=True)
        alias = self.short / app.lower()
        alias.symlink_to(profile, target_is_directory=True)
        for name in ('config', 'cache', 'data', 'state', 'runtime'):
            (profile / name).mkdir(exist_ok=True, mode=0o700)
        if app == 'WPS':
            # Same isolated EULA preference used by the existing WPS runtime;
            # no desktop recovery/session/account settings are copied.
            folder = profile / 'config/Kingsoft'
            folder.mkdir(parents=True)
            (folder / 'Office.conf').write_text(
                '[6.0]\nwpsoffice\\Application%20Settings\\AppComponentMode=prome_fushion\n'
                'common\\AcceptedEULA=true\n')
        if app == 'SHOTCUT':
            folder = profile / 'config/Meltytech'
            folder.mkdir(parents=True)
            (folder / 'Shotcut.conf').write_text('[encode]\nfreeSpaceCheck=false\n')
        env = ['env', 'LANG=en_US.UTF-8', 'LC_ALL=en_US.UTF-8',
               'QT_AUTO_SCREEN_SCALE_FACTOR=0', 'QT_SCALE_FACTOR=1',
               'LIBGL_ALWAYS_SOFTWARE=1', 'GIO_USE_VFS=local']
        if app == 'WPS':
            env = ['env', 'LANG=en_US.UTF-8', 'LC_ALL=en_US.UTF-8']
        if app == 'SHOTCUT':
            # Avoid the GTK native file chooser enumerating stale GVfs mounts
            # left by unrelated old experiments. Use Qt's own file dialog.
            env += ['QT_QPA_PLATFORMTHEME=generic', 'QT_STYLE_OVERRIDE=Fusion',
                    'XDG_CURRENT_DESKTOP=X-Generic', 'DESKTOP_SESSION=generic']
        env += [f'XDG_{key}_HOME={alias}/{folder}' for key, folder in
                [('CONFIG', 'config'), ('CACHE', 'cache'), ('DATA', 'data'), ('STATE', 'state')]]
        env += [f'XDG_RUNTIME_DIR={alias}/runtime']
        self.commands[app] = command
        log = profile / 'launcher.log'
        with log.open('w') as f:
            subprocess.run([sys.executable, str(ROOT / 'test/test/lsapp_30_gui.py'), 'start',
                            '--output-dir', str(self.out), '--app', app, '--', *env, *command],
                           stdout=f, stderr=subprocess.STDOUT, check=True, timeout=45)
        self.focus(app)

    def inventory(self, app):
        rows = {r['pid']: r for r in gui.process_inventory(self.out, app)}
        # Short XDG aliases must still detect helpers migrated out of the unit.
        prefix = ('XDG_CONFIG_HOME=' + str(self.short / app.lower() / 'config')).encode()
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if prefix not in (proc / 'environ').read_bytes().split(b'\0'):
                    continue
                pid = int(proc.name)
                rows[pid] = dict(pid=pid, cgroup=(proc / 'cgroup').read_text().strip(),
                                 command=(proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace'))
            except OSError:
                continue
        return list(rows.values())

    def audit(self, app):
        rows = self.inventory(app)
        outside = [r for r in rows if not self.owned(app, r)]
        result = dict(processes=rows, outside_original_unit=outside)
        write_json(self.out / app / 'process-audit.json', result)
        if not rows or outside:
            raise RuntimeError('Process ownership failed: ' + app)
        return len(rows)

    def close(self):
        results = {}
        for app in reversed(self.attempted):
            before = self.inventory(app)
            try:
                stopped = subprocess.run(['systemctl', '--user', 'stop', gui.unit(self.out, app)],
                                         capture_output=True, text=True, timeout=20)
                stop_result = stopped.returncode
            except subprocess.TimeoutExpired:
                stop_result = 'timeout'
            remaining = self.inventory(app)
            result = dict(passed=not remaining, before_pids=[r['pid'] for r in before],
                          remaining=remaining, stop_returncode=stop_result)
            write_json(self.out / app / 'cleanup.json', result)
            results[app] = result
        if all(r['passed'] for r in results.values()):
            shutil.rmtree(self.short)
        return results


def prepare(out):
    fixtures = out / 'fixtures'
    fixtures.mkdir()
    shutil.copyfile(ROOT / 'test/samples/wps/word_0040_fixture.docx', fixtures / 'phase1.docx')
    shutil.copyfile(ROOT / 'test/samples/wps/video_5s_test.mp4', fixtures / 'clip.mp4')
    (fixtures / 'note.txt').write_text('PARP phase one note\n')
    (fixtures / 'extracted').mkdir()
    with zipfile.ZipFile(fixtures / 'phase1.zip', 'w') as archive:
        archive.writestr('payload.txt', PAYLOAD)
    write_json(out / 'fixture-hashes-before.json', {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixtures.iterdir() if p.is_file()})
    return fixtures


def word_contains(path, token):
    with zipfile.ZipFile(path) as doc:
        root = ET.fromstring(doc.read('word/document.xml'))
    return token in ''.join(root.itertext())


def dismiss_wps_popups(replay):
    app = "WPS"
    # Initial modal prompts can arrive after the embedded editor. Require
    # a quiet interval and close their own controls instead of sending
    # document keystrokes to a late popup.
    quiet_since = time.monotonic()
    deadline = quiet_since + 15
    while time.monotonic() < deadline:
        closed = False
        # The default-app prompt can be modal over the font check. Close it
        # first; waiting synchronously to activate the blocked parent can hang.
        rows=sorted(replay.state()['windows'],key=lambda r:r['net_wm_name']!='WPS Office')
        for row in rows:
            if not replay.owned(app, row) or row['net_wm_name'] not in {'System Check', 'WPS Office'}:
                continue
            wid = row['window_id']
            geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                                 capture_output=True, text=True)
            if geo.returncode:
                continue  # Startup prompt can disappear between the snapshot and geometry lookup.
            values = dict(line.split('=', 1) for line in geo.stdout.splitlines() if '=' in line)
            width, height = int(values['WIDTH']), int(values['HEIGHT'])
            if width > 900 or height > 450:
                continue
            subprocess.run(['xdotool', 'windowactivate', wid], check=True, timeout=3)
            if row['net_wm_name'] == 'System Check':
                replay.input(app, 'dismiss-font-check', 'mousemove', '--window', wid,
                             str(width - 45), str(height - 25), 'click', '1')
            else:
                # This build ignores Escape on its custom default-app dialog.
                # Use the close control, never OK (which changes file defaults).
                replay.input(app, 'dismiss-default-app-prompt', 'mousemove', '--window', wid,
                             str(width - 16), '15', 'click', '1')
            closed = True
        if closed:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= 3:
            break
        time.sleep(.2)
    else:
        raise RuntimeError('WPS startup prompts did not close')


def operate(replay, app, fixtures):
    if app == 'WPS':
        wait_for(lambda: any('phase1.docx' in r['net_wm_name'] and replay.owned(app, r)
                             for r in replay.state()['windows']), 'WPS document')
        # The title appears before the editor is usable. Initial font/default
        # handler prompts are closed, never confirmed to change OS defaults.
        def embedded_editor():
            roots = {int(r['window_id'], 16) for r in replay.state()['windows']}
            found = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', 'phase1.docx'],
                                   capture_output=True, text=True)
            for wid in found.stdout.split():
                if int(wid) in roots:
                    continue
                owner = subprocess.run(['xdotool', 'getwindowpid', wid], capture_output=True, text=True)
                if not owner.stdout.strip().isdigit():
                    continue
                cg = Path('/proc') / owner.stdout.strip() / 'cgroup'
                if not replay.owned(app, {'cgroup': cg.read_text().strip()}):
                    continue
                geo = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                                     capture_output=True, text=True, check=True)
                values = dict(line.split('=', 1) for line in geo.stdout.splitlines() if '=' in line)
                if int(values['WIDTH']) > 800 and int(values['HEIGHT']) > 400:
                    return wid
            return None
        editor = wait_for(embedded_editor, 'loaded embedded WPS editor', 60)
        dismiss_wps_popups(replay)
        windows = [r for r in replay.state()['windows'] if replay.owned(app, r)
                   and 'phase1.docx' in r['net_wm_name']]
        subprocess.run(['xdotool', 'windowactivate', '--sync', windows[-1]['window_id']], check=True)
        subprocess.run(['xdotool', 'windowfocus', editor], check=True)
        replay.key(app, 'Escape', 'ctrl+End', 'Return')
        replay.type(app, TOKEN)
        replay.key(app, 'ctrl+s')
        wait_for(lambda: word_contains(fixtures / 'phase1.docx', TOKEN), 'saved WPS content')
        replay.key(app, 'Escape')
        dismiss_wps_popups(replay)
        replay.focus(app)
        return dict(operation='append_and_save_docx', saved_token=TOKEN)
    if app == 'MOUSEPAD':
        replay.key(app, 'ctrl+End')
        replay.type(app, TOKEN)
        replay.key(app, 'ctrl+s')
        wait_for(lambda: TOKEN in (fixtures / 'note.txt').read_text(), 'saved Mousepad content')
        return dict(operation='append_and_save_text', saved_token=TOKEN)
    if app == 'FILE_ROLLER':
        wid = replay.focus(app)
        subprocess.run(['wmctrl', '-ir', wid, '-b', 'add,maximized_vert,maximized_horz'], check=True)
        time.sleep(.5)
        replay.input(app, 'extract-button', 'mousemove', '45', '35', 'click', '1')
        wait_for(lambda: replay.active(app)['net_wm_name'] == 'Extract', 'archive extract dialog')
        replay.key(app, 'ctrl+l')
        replay.type(app, str(fixtures / 'extracted') + '/')
        replay.key(app, 'Return')
        replay.input(app, 'confirm-extract', 'mousemove', '1140', '872', 'click', '1')
        payload = fixtures / 'extracted/payload.txt'
        wait_for(lambda: payload.exists() and payload.read_bytes() == PAYLOAD, 'extracted payload')
        replay.key(app, 'Escape')
        return dict(operation='gui_extract_zip', payload_sha256=hashlib.sha256(PAYLOAD).hexdigest())
    if app == 'SHOTCUT':
        return operate_shotcut(replay, fixtures)
    raise ValueError(app)


def operate_shotcut(replay, fixtures):
    app = 'SHOTCUT'
    wait_for(lambda: any('Untitled - Shotcut' == r['net_wm_name'] and replay.owned(app, r)
                         for r in replay.state()['windows']), 'Shotcut editor', 30)
    time.sleep(3)
    wid = replay.focus(app)
    subprocess.run(['wmctrl', '-ir', wid, '-b', 'add,maximized_vert,maximized_horz'], check=True)
    replay.key(app, 'k')
    replay.key(app, 'ctrl+2')
    replay.key(app, 'a')
    # Append generates thumbnails/waveforms and pumps a nested Qt event loop.
    # Saving during that loop can deadlock its finishing dialog.
    time.sleep(4)
    wait_for(lambda: subprocess.run(['xdotool', 'search', '--onlyvisible', '--name',
                                     '^Append to Timeline$'], capture_output=True).returncode != 0,
             'timeline append completed', 30)
    project = fixtures / 'edit.mlt'
    replay.key(app, 'ctrl+shift+s')
    wait_for(lambda: replay.active(app)['net_wm_name'] == 'Save XML', 'Shotcut Save XML dialog')
    replay.type(app, str(project))
    replay.key(app, 'Return')
    wait_for(lambda: project.exists() and timeline_entries(project), 'saved timeline')
    before = timeline_entries(project)
    if len(before) != 1:
        raise RuntimeError('Expected one initial video clip')
    replay.key(app, 'Home')
    replay.key(app, 'shift+Next')
    replay.key(app, 's')
    time.sleep(2)
    replay.key(app, 'ctrl+s')
    wait_for(lambda: len(timeline_entries(project)) == 2, 'two split clips in saved project')
    after = timeline_entries(project)
    audit = validate_split(before, after)
    # Preview and pause, with frame evidence retained.
    replay.key(app, 'Home')
    replay.key(app, 'l')
    time.sleep(1)
    replay.key(app, 'k')
    replay.key(app, 'ctrl+e')
    # Fixed 1280x900 private desktop, maximized Shotcut 22.01.30/Fusion.
    replay.input(app, 'export-file', 'mousemove', '303', '584', 'click', '1')
    wait_for(lambda: replay.active(app)['net_wm_name'] == 'Export File', 'Shotcut export filename dialog')
    exported = fixtures / 'export.mp4'
    replay.type(app, str(exported))
    replay.key(app, 'Return')
    # Observe an actual export process in the application unit while running.
    observed = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        observed += [r for r in replay.inventory(app)
                     if any(Path(part).name in {'melt', 'melt-7', 'qmelt', 'ffmpeg'}
                            for part in r['command'].split())]
        if exported.exists():
            check = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                    'format=duration:stream=codec_type,width,height',
                                    '-of', 'json', str(exported)], capture_output=True, text=True)
            if check.returncode == 0:
                media = json.loads(check.stdout)
                if float(media.get('format', {}).get('duration', 0)) > 0:
                    break
        time.sleep(.2)
    else:
        raise RuntimeError('Shotcut export did not produce a readable video')
    if not observed or any(not replay.owned(app, r) for r in observed):
        raise RuntimeError('No export subprocess observed in Shotcut cgroup')
    expected_s = frame_number(before[0]['out']) / 30 + 1 / 30
    if abs(float(media['format']['duration']) - expected_s) > .2:
        raise RuntimeError('Export duration does not match the edited timeline')
    if not any(s.get('codec_type') == 'video' for s in media['streams']):
        raise RuntimeError('Export contains no video stream')
    # Decode all frames, rather than accepting a header-only/truncated MP4.
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(exported),
                    '-f', 'null', '-'], check=True, capture_output=True, timeout=30)
    write_json(replay.out / app / 'export-audit.json', dict(media=media, processes=observed))
    replay.record(app, 'export-complete')
    return dict(operation='timeline_split_preview_save_export', split=audit, export=media)


def timeline_entries(path):
    root = ET.parse(path).getroot()
    return [dict(entry.attrib) for playlist in root.findall('playlist')
            if playlist.find("property[@name='shotcut:video']") is not None
            for entry in playlist.findall('entry')]


def frame_number(value):
    if ':' not in value:
        return int(value)
    hours, minutes, seconds = value.split(':')
    return round((3600 * int(hours) + 60 * int(minutes) + float(seconds)) * 30)


def validate_split(before, after):
    if len(before) != 1 or len(after) != 2:
        raise ValueError('Expected a one-to-two split')
    original = (frame_number(before[0]['in']), frame_number(before[0]['out']))
    parts = [(frame_number(e['in']), frame_number(e['out'])) for e in after]
    if (parts[0][0] != original[0] or parts[1][1] != original[1]
            or parts[0][1] + 1 != parts[1][0]
            or any(start > end for start, end in parts)):
        raise ValueError('Split lost, duplicated, or reordered frames')
    if parts[1][0] != 60:
        raise ValueError('Expected deterministic split at frame 60 (2 seconds at 30 fps)')
    return dict(original_frames=list(original), split_frames=[list(p) for p in parts])


def run(out, apps):
    scope = validate_config()
    if shutil.disk_usage(out.parent if out.parent.exists() else ROOT).free < 1024 ** 3:
        raise RuntimeError('At least 1 GiB free disk space is required for the bounded fixture run')
    out.mkdir(parents=True, exist_ok=False)
    fixtures = prepare(out)
    replay = Replay(out)
    results = {}
    cleanup = {}
    commands = {
        'WPS': ['/opt/kingsoft/wps-office/office6/wpsoffice', '/prometheus', str(fixtures / 'phase1.docx')],
        'FILE_ROLLER': ['file-roller', str(fixtures / 'phase1.zip')],
        'MOUSEPAD': ['mousepad', '--disable-server', str(fixtures / 'note.txt')],
        'SHOTCUT': ['shotcut', str(fixtures / 'clip.mp4')],
    }
    with desktop_api.desktop('isolated', out):
        write_json(out / 'desktop-env.json', {key: os.environ[key] for key in
                   ['DISPLAY', 'XAUTHORITY', 'GDK_BACKEND', 'QT_QPA_PLATFORM',
                    'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP'] if key in os.environ})
        try:
            for app in apps:
                print('Starting ' + app, flush=True)
                try:
                    replay.start(app, commands[app])
                    operation = operate(replay, app, fixtures)
                    count = replay.audit(app)
                    results[app] = dict(status='CONTENT_PASS', operation=operation, process_count=count)
                    print(app + ' content and ownership passed', flush=True)
                except Exception as exc:
                    results[app] = dict(status='FAILED', error=repr(exc))
                    replay.record(app, 'failure')
                    print(app + ': ' + repr(exc), flush=True)
                write_json(out / 'progress.json', results)
            good = [a for a in apps if results[a]['status'] == 'CONTENT_PASS']
            # Actual cross-application focus returns, not repeated activation
            # of the already-active window.
            for app in reversed(good):
                previous = replay.state()['active_window']
                replay.focus(app)
                results[app]['focus_switch_observed'] = replay.state()['active_window'] != previous
            for app in good:
                previous = replay.state()['active_window']
                replay.focus(app)
                results[app]['focus_switch_observed'] |= replay.state()['active_window'] != previous
                results[app]['process_count'] = replay.audit(app)
                if len(good) > 1 and not results[app]['focus_switch_observed']:
                    raise RuntimeError('No cross-app focus return: ' + app)
            scope['slice'] = 'parp-30-gui.slice'
            for app in scope['apps']:
                if app['app_key'] in apps:
                    service = gui.unit(out, app['app_key'])
                    app.update(binding_scope_names=[service], scope_name=service,
                               unit_name=service.removesuffix('.service'))
            scope['status'] = 'run_specific_automation_bindings_predictions_disabled'
            write_json(out / 'runtime_app_scope.json', scope)
        except Exception as exc:
            for result in results.values():
                result.update(status='FAILED', integration_error=repr(exc))
            raise
        finally:
            cleanup = replay.close()
            for app, result in results.items():
                result['cleanup_passed'] = cleanup.get(app, {}).get('passed', False)
                if result['status'] == 'CONTENT_PASS' and result['cleanup_passed']:
                    result['status'] = 'GUI_ACCEPTANCE_PASS'
                elif not result['cleanup_passed']:
                    result['status'] = 'FAILED'
            write_json(out / 'acceptance.json', dict(apps=results,
                       full_phase1=len(results) == 4 and all(r['status'] == 'GUI_ACCEPTANCE_PASS' for r in results.values()),
                       prediction_tested=False, kernel_written=False, performance_tested=False))
    return all(r['status'] == 'GUI_ACCEPTANCE_PASS' for r in results.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--apps', nargs='+', choices=APPS, default=list(APPS))
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    if args.check_config:
        validate_config()
        print('30 application IDs, vocabulary and runtime parser passed')
        return
    if args.output_dir is None:
        parser.error('--output-dir is required for GUI replay')
    if len(set(args.apps)) != len(args.apps):
        parser.error('--apps must not contain duplicates')
    if not run(args.output_dir.resolve(), args.apps):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
