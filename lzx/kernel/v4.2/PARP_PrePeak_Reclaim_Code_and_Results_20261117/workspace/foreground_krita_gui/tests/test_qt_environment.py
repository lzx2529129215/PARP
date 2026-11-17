"""Environment/subprocess boundary tests; these do not start real Krita or X11."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from oomlab_krita_gui.config import ROOT,SafetyError,load
from oomlab_krita_gui.environment import check
from oomlab_krita_gui.process_manager import Process,QT_PATH_KEYS,SESSION_KEYS,qt_environment,require_x11_platform


def polluted():
    return dict(PATH=os.defpath,HOME='/original/home',DISPLAY=':12.0',XAUTHORITY='/session/private-auth',
                XDG_RUNTIME_DIR='/run/user/1000',DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus',
                QT_QPA_PLATFORM_PLUGIN_PATH='/private/.venv/lib/python3.12/site-packages/cv2/qt/plugins',
                QT_PLUGIN_PATH='/private/other-qt',QT_QPA_FONTDIR='/private/cv2/qt/fonts',
                QT_QPA_PLATFORM='xcb',XDG_DATA_DIRS='/usr/local/share:/usr/share')


class QtEnvironmentTests(unittest.TestCase):
    def test_copy_removes_all_paths_preserves_session_and_parent(self):
        source=polluted(); before=source.copy()
        clean,audit=qt_environment(source)
        self.assertEqual(source,before)
        for key in QT_PATH_KEYS: self.assertNotIn(key,clean)
        for key in (*SESSION_KEYS,'XDG_DATA_DIRS','QT_QPA_PLATFORM'): self.assertEqual(clean[key],source[key])
        self.assertTrue(all(audit['session_unchanged'].values()))
        for key in QT_PATH_KEYS:
            self.assertTrue(audit['before'][key]['present']); self.assertFalse(audit['after'][key]['present'])

    def test_absent_paths_stay_absent_no_hardcoded_plugin_directory(self):
        clean,audit=qt_environment(dict(DISPLAY=':12.0'))
        self.assertEqual(clean,dict(DISPLAY=':12.0')); self.assertEqual(audit['removed_keys'],[])
        require_x11_platform(clean)

    def test_qt_audit_never_logs_raw_paths_session_addresses_or_secrets(self):
        source=polluted(); source['UNRELATED_SECRET']='not-for-logs'
        _,audit=qt_environment(source); text=json.dumps(audit)
        for value in source.values():
            if value not in ('xcb',): self.assertNotIn(value,text)
        self.assertNotIn('UNRELATED_SECRET',text)

    def test_only_native_x11_platform_is_accepted(self):
        for platform in [None,'','xcb']:
            with self.subTest(platform=platform): require_x11_platform({} if platform is None else dict(QT_QPA_PLATFORM=platform))
        for platform in ['offscreen','minimal','minimalegl','vnc','wayland','eglfs','xcb;offscreen','xcb:foo','surprise']:
            with self.subTest(platform=platform),self.assertRaises(SafetyError): require_x11_platform(dict(QT_QPA_PLATFORM=platform))

    def test_launch_boundary_cleans_pollution_and_preserves_parent(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,polluted(),clear=True):
            before=dict(os.environ); run=Path(d); p=Process(run); child=Mock(pid=123)
            with patch('oomlab_krita_gui.process_manager.existing',return_value=[]), \
                 patch('oomlab_krita_gui.process_manager.identity',return_value=dict(pid=123,starttime=456)), \
                 patch('oomlab_krita_gui.process_manager.subprocess.Popen',return_value=child) as popen:
                try: p.launch()
                finally:
                    if hasattr(p,'log'): p.log.close()
            self.assertEqual(dict(os.environ),before)
            self.assertEqual(popen.call_args.args[0],['krita'])
            env=popen.call_args.kwargs['env']
            for key in QT_PATH_KEYS: self.assertNotIn(key,env)
            for key in SESSION_KEYS: self.assertEqual(env[key],before[key])
            self.assertEqual(env['XDG_DATA_DIRS'],before['XDG_DATA_DIRS'])
            self.assertEqual(env['HOME'],str(run/'private-home'))
            audit=json.loads((run/'qt-environment.json').read_text())
            self.assertTrue(audit['platform_allowed'])
            self.assertEqual(set(audit['removed_keys']),set(QT_PATH_KEYS))

    def test_rejected_platform_writes_audit_but_never_starts_child(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{**polluted(),'QT_QPA_PLATFORM':'offscreen'},clear=True):
            p=Process(Path(d))
            with patch('oomlab_krita_gui.process_manager.existing',return_value=[]), \
                 patch('oomlab_krita_gui.process_manager.subprocess.Popen') as popen:
                with self.assertRaises(SafetyError): p.launch()
            popen.assert_not_called()
            self.assertFalse(json.loads((Path(d)/'qt-environment.json').read_text())['platform_allowed'])
            self.assertEqual(os.environ['QT_QPA_PLATFORM'],'offscreen')

    def test_clean_environment_reaches_a_real_non_gui_python_child(self):
        with patch.dict(os.environ,polluted(),clear=True):
            before=dict(os.environ); clean,_=qt_environment()
            keys=(*QT_PATH_KEYS,*SESSION_KEYS)
            code='import json,os; print(json.dumps({k:os.environ.get(k) for k in '+repr(keys)+'}))'
            child=subprocess.run([sys.executable,'-c',code],env=clean,capture_output=True,text=True,check=True,timeout=10)
            received=json.loads(child.stdout)
            for key in QT_PATH_KEYS: self.assertIsNone(received[key])
            for key in SESSION_KEYS: self.assertEqual(received[key],before[key])
            self.assertEqual(dict(os.environ),before)

    def test_version_preflight_uses_same_clean_environment(self):
        with patch.dict(os.environ,polluted(),clear=True), \
             patch('oomlab_krita_gui.environment.shutil.which',return_value='/usr/bin/tool'), \
             patch('oomlab_krita_gui.environment.importlib.util.find_spec',return_value=object()), \
             patch('oomlab_krita_gui.environment.os.geteuid',return_value=1000):
            before=dict(os.environ)
            def command(args,**kwargs):
                if args[0]=='krita':
                    for key in QT_PATH_KEYS: self.assertNotIn(key,kwargs['env'])
                    self.assertEqual(kwargs['env']['DISPLAY'],':12.0')
                    return 'krita 5.2.2'
                return 'eng' if args[0]=='tesseract' else 'mock X11'
            with patch('oomlab_krita_gui.environment.command',side_effect=command): result=check(load(ROOT/'configs/gui.json'))
            self.assertTrue(result['ok']); self.assertEqual(dict(os.environ),before)

    def test_ubuntu_2204_version_requires_exact_supported_release(self):
        for version,accepted in [('krita 5.0.2',True),('krita 5.2.2',True),
                                 ('krita 5.0.20',False),('krita 5.1.0',False)]:
            with self.subTest(version=version),patch.dict(os.environ,polluted(),clear=True), \
                 patch('oomlab_krita_gui.environment.shutil.which',return_value='/usr/bin/tool'), \
                 patch('oomlab_krita_gui.environment.importlib.util.find_spec',return_value=object()), \
                 patch('oomlab_krita_gui.environment.os.geteuid',return_value=1000):
                def command(args,**kwargs):
                    return version if args[0]=='krita' else 'eng' if args[0]=='tesseract' else 'mock X11'
                with patch('oomlab_krita_gui.environment.command',side_effect=command):
                    self.assertEqual(check(load(ROOT/'configs/gui.json'))['ok'],accepted)
