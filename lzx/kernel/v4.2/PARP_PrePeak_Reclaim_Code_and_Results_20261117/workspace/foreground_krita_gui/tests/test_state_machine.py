import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from oomlab_krita_gui.config import SafetyError
from oomlab_krita_gui.state_machine import Machine,STATES,wait_for
from oomlab_krita_gui.report import generate


class FakeDesktop:
    def __init__(self,run,fail=None): self.run=run; self.calls=[]; self.fail=fail; self.windows=Mock()
    def capture(self,name):
        from PIL import Image
        path=self.run/name; Image.new('RGB',(16,16),'blue').save(path); return name
    def __getattr__(self,name):
        if name.upper() not in STATES: raise AttributeError(name)
        def step():
            self.calls.append(name.upper())
            if name.upper()==self.fail: raise RuntimeError('fixture failure')
            return dict(ok=True,returncode=0) if name=='verify_exit' else dict(ok=True)
        return step


class StateTests(unittest.TestCase):
    def test_startup_only_never_opens_asset_or_claims_complete_gui(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); g=FakeDesktop(run)
            m=dict(kind='krita_external_gui',startup_only=True,krita_pid=77,window_id='0x1',
                   environment=dict(ok=True),asset=dict(sha256='a'*64))
            self.assertTrue(Machine(g,run,m).execute())
            self.assertEqual(g.calls,list(STATES[:2])); self.assertFalse(m['success'])
            self.assertEqual(m['status'],'STARTUP_VERIFIED'); self.assertTrue(m['scene_preserved'])
            result=generate(run)
            self.assertFalse(result['valid']); self.assertEqual(result['startup_test'],'PASS')
            self.assertEqual(result['gui_test'],'NOT_RUN')
            (run/'01_LAUNCH_KRITA_before.png').unlink()
            self.assertEqual(generate(run)['status'],'INVALID_EVIDENCE')

    def test_startup_failure_never_opens_document(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); g=FakeDesktop(run,'VERIFY_KRITA'); g.windows.all.return_value=[]
            m=dict(startup_only=True)
            self.assertFalse(Machine(g,run,m).execute())
            self.assertEqual(g.calls,list(STATES[:2])); self.assertFalse(m['success'])
            self.assertTrue(m['scene_preserved']); self.assertNotEqual(generate(run)['startup_test'],'PASS')

    def test_full_order_report_and_screenshots(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); g=FakeDesktop(run); m=dict(kind='krita_external_gui',krita_pid=77,window_id='0x1',environment=dict(ok=True),asset=dict(sha256='a'*64))
            self.assertTrue(Machine(g,run,m).execute()); self.assertEqual(g.calls,list(STATES))
            rows=[json.loads(l) for l in (run/'stages.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),16)
            self.assertTrue(all(r['screenshot_before'] and r['screenshot_after'] and r['verification']['ok'] for r in rows))
            self.assertTrue(generate(run)['valid'])  # Synthetic logic only; temporary report.

    def test_failure_stops_later_actions_and_preserves_scene(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); g=FakeDesktop(run,'VERIFY_ZOOM_IN'); g.windows.all.return_value=[]; m={}
            self.assertFalse(Machine(g,run,m).execute())
            self.assertNotIn('CREATE_LAYER',g.calls); self.assertNotIn('QUIT_KRITA',g.calls)
            self.assertTrue(m['scene_preserved']); self.assertTrue((run/'failure-windows.json').exists())
            self.assertFalse(generate(run)['valid'])

    def test_timeout_and_identity_loss(self):
        with self.assertRaises(TimeoutError): wait_for(lambda:dict(ok=False),0,.001)
        probe=Mock(side_effect=SafetyError('wrong PID'))
        with self.assertRaises(SafetyError): wait_for(probe,10,.1)
        probe.assert_called_once()

    def test_stability_requires_repeated_positive_observations(self):
        probe=Mock(side_effect=[dict(ok=True),dict(ok=False),dict(ok=True),dict(ok=True)])
        with patch('oomlab_krita_gui.state_machine.time.sleep'):
            self.assertTrue(wait_for(probe,1,.01)['ok'])
        self.assertEqual(probe.call_count,4)

    def test_capture_failure_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); g=FakeDesktop(run); g.capture=Mock(side_effect=OSError('disk error')); g.windows.all.return_value=[]
            self.assertFalse(Machine(g,run,{}).execute()); self.assertEqual(g.calls,[])
