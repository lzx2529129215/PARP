import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from wps_runtime import prepare_runtime,EXECUTABLES


class WPSRuntimeTests(unittest.TestCase):
    def test_private_config_changes_without_modifying_installed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'installed';(source/'cfgs').mkdir(parents=True)
            text='[Product]\nEnableAccount=false\nEnableCloudDocs=false\n[Support]\nEnableAccount=false\n'
            (source/'cfgs/oem.ini').write_text(text)
            for name in EXECUTABLES:(source/name).write_bytes(('unchanged '+name).encode())
            (source/'library.so').write_bytes(b'library')
            dest=prepare_runtime(root/'private',source)
            self.assertEqual((source/'cfgs/oem.ini').read_text(),text)
            self.assertEqual((dest/'cfgs/oem.ini').read_text(),text.replace('EnableAccount=false','EnableAccount=true'))
            self.assertFalse((dest/'cfgs').is_symlink())
            for name in EXECUTABLES:
                self.assertFalse((dest/name).is_symlink())
                self.assertEqual((dest/name).read_bytes(),(source/name).read_bytes())
            self.assertTrue((dest/'library.so').is_symlink())
            evidence=json.loads((dest/'parp-runtime-manifest.json').read_text())
            self.assertNotEqual(evidence['source_oem_sha256'],evidence['runtime_oem_sha256'])

    def test_unknown_oem_format_fails_before_creating_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'installed';(source/'cfgs').mkdir(parents=True)
            (source/'cfgs/oem.ini').write_text('[Product]\n')
            with self.assertRaisesRegex(RuntimeError,'Unsupported'):prepare_runtime(root/'private',source)
            self.assertFalse((root/'private').exists())

    def test_modal_default_prompt_is_closed_before_blocked_font_dialog(self):
        import itertools
        import source20_phase1_gui as gui
        windows=[dict(window_id='font',net_wm_name='System Check'),dict(window_id='default',net_wm_name='WPS Office')]
        replay=Mock();replay.owned.return_value=True;replay.state.side_effect=lambda:{'windows':list(windows)}
        def close(app,label,*args):
            wid=args[args.index('--window')+1]
            windows[:]=[w for w in windows if w['window_id']!=wid]
        replay.input.side_effect=close
        with patch.object(gui.subprocess,'run',return_value=Mock(returncode=0,stdout='WIDTH=750\nHEIGHT=250\n')) as run,\
             patch.object(gui.time,'sleep'),patch.object(gui.time,'monotonic',side_effect=itertools.count(step=.5)):
            gui.dismiss_wps_popups(replay)
        self.assertEqual([c.args[1] for c in replay.input.call_args_list],['dismiss-default-app-prompt','dismiss-font-check'])
        self.assertEqual(replay.input.call_args_list[0].args[-4:],('734','15','click','1'))
        replay.key.assert_not_called()
        self.assertTrue(all('--sync' not in c.args[0] for c in run.call_args_list))


if __name__=='__main__':unittest.main()
