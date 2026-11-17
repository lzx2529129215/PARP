import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock,patch
from oomlab_krita_gui.cli import main
from oomlab_krita_gui.config import ROOT,load,SafetyError
from oomlab_krita_gui.process_manager import Process
from oomlab_krita_gui.report import generate


class PreflightTests(unittest.TestCase):
    def test_environment_failure_cannot_launch(self):
        with tempfile.TemporaryDirectory() as d,patch('oomlab_krita_gui.cli.check',return_value=dict(ok=False,errors=['no display'])):
            with patch('oomlab_krita_gui.gui_controller.Controller') as gui,redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run','--outputs',d]),1)
            gui.assert_not_called()
            run=next(Path(d).iterdir()); meta=json.loads((run/'metadata.json').read_text())
            self.assertFalse(meta['success']); self.assertEqual(meta['status'],'NOT_RUN')

    def test_unpinned_asset_cannot_launch(self):
        with tempfile.TemporaryDirectory() as d,patch('oomlab_krita_gui.cli.check',return_value=dict(ok=True,errors=[])), \
             patch('oomlab_krita_gui.cli.discover',return_value=[]),redirect_stdout(io.StringIO()):
            with patch('oomlab_krita_gui.gui_controller.Controller') as gui:
                self.assertEqual(main(['run','--outputs',d]),1)
            gui.assert_not_called()
            self.assertTrue((next(Path(d).iterdir())/'asset-candidates.json').exists())

    def test_success_flag_without_real_evidence_is_not_valid(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); (run/'metadata.json').write_text(json.dumps(dict(success=True,status='COMPLETE')))
            self.assertFalse(generate(run)['valid'])

    def test_forwarded_process_fails_without_touching_existing_instance(self):
        with tempfile.TemporaryDirectory() as d:
            p=Process(Path(d)); p.child=Mock();p.child.poll.return_value=0
            with self.assertRaises(SafetyError): p.verify()
            p.child.terminate.assert_not_called(); p.child.kill.assert_not_called()

    def test_pid_starttime_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Process(Path(d)); p.child=Mock();p.child.poll.return_value=None
            p.info=dict(pid=77,starttime=100)
            with patch('oomlab_krita_gui.process_manager.identity',return_value=dict(pid=77,starttime=99,ppid=1,state='S',exe='/usr/bin/krita')):
                with self.assertRaises(SafetyError): p.verify()
