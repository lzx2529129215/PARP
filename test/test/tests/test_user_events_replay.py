"""Regression checks for failures observed during real workbook calibration."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import user_events_replay as U


class ReplayGuardTests(unittest.TestCase):
    def test_hidden_tab_cannot_reuse_stale_visible_telemetry(self):
        pages=object.__new__(U.LocalPages)
        pages.events=[dict(id='5',visible=True,y=300),dict(id='5',visible=False,y=300)]
        self.assertIsNone(pages.latest(5))
        pages.events.append(dict(id='5',visible=True,y=320))
        self.assertEqual(pages.latest(5)['y'],320)

    def test_tab_selection_uses_page_identity_not_assumed_tab_order(self):
        replay=object.__new__(U.Replay);pages=object.__new__(U.LocalPages)
        pages.events=[dict(id='10',visible=True),dict(id='20',visible=False),dict(id='30',visible=False)]
        replay.pages=pages;replay.tabs={'FIREFOX':[10,20,30]};replay.page={'FIREFOX':10}
        actual=[10,30,20];position=[0];keys=[]
        def cycle(app,key):
            keys.append(key);pages.events.append(dict(id=str(actual[position[0]]),visible=False))
            position[0]=(position[0]+1)%3
            pages.events.append(dict(id=str(actual[position[0]]),visible=True))
        replay.key=cycle
        with patch.object(U.time,'sleep'):
            self.assertEqual(replay.select_page('FIREFOX',20)['id'],'20')
        self.assertEqual(keys,['ctrl+Tab','ctrl+Tab'])


    def test_tab_selection_reload_fallback_marks_substitution(self):
        replay=object.__new__(U.Replay);pages=object.__new__(U.LocalPages)
        pages.events=[dict(id='10',visible=True)]
        pages.url=Mock(return_value='http://127.0.0.1:1/page/20')
        replay.pages=pages;replay.tabs={'FIREFOX':[10,20]};replay.page={'FIREFOX':10}
        replay.focus=Mock();replay.key=Mock();replay.type=Mock()
        def wait_for(fn,label,timeout):
            self.assertEqual(label,'fallback browser page loaded')
            pages.events.append(dict(id='20',visible=True))
            return fn()
        with patch.object(U.P,'wait_for',side_effect=wait_for), patch.object(U.time,'sleep'):
            proof=replay.select_page('FIREFOX',20,fallback_reload=True)
        self.assertTrue(proof['tab_reload_fallback'])
        pages.url.assert_called_once_with(20,'article')
        replay.type.assert_called_once_with('FIREFOX','http://127.0.0.1:1/page/20')


    def test_browser_scroll_retries_then_records_unverified_fallback(self):
        replay=object.__new__(U.Replay);pages=object.__new__(U.LocalPages)
        pages.events=[dict(id='20',visible=True,y=100,received_at=1.0)]
        replay.pages=pages;replay.page={'FIREFOX':20}
        replay.input=Mock();replay.focus=Mock()
        event=dict(target_app_key='FIREFOX',operation='web_scroll',params={'direction':1},excel_row=21)
        with patch.object(U.P,'wait_for',side_effect=[RuntimeError('no delta'),RuntimeError('no delta')]), patch.object(U.time,'sleep'):
            detail=replay.web(event)
        self.assertEqual(detail['verification'],'scroll_offset_unverified')
        self.assertEqual(replay.input.call_count,2)
        replay.focus.assert_called_once_with('FIREFOX')

    def test_browser_scroll_retry_success_reports_retry_count(self):
        replay=object.__new__(U.Replay);pages=object.__new__(U.LocalPages)
        pages.events=[dict(id='20',visible=True,y=100,received_at=1.0)]
        replay.pages=pages;replay.page={'FIREFOX':20}
        replay.input=Mock();replay.focus=Mock()
        event=dict(target_app_key='FIREFOX',operation='web_scroll',params={'direction':1},excel_row=21)
        def wait_for(fn,label,timeout):
            if replay.input.call_count==1:raise RuntimeError('no delta')
            pages.events.append(dict(id='20',visible=True,y=200,received_at=999.0))
            return fn()
        with patch.object(U.P,'wait_for',side_effect=wait_for), patch.object(U.time,'sleep'):
            detail=replay.web(event)
        self.assertEqual(detail['verification'],'scroll_offset')
        self.assertEqual(detail['retries'],1)

    def test_file_browser_child_editor_is_not_an_allowed_input_target(self):
        replay=object.__new__(U.Replay)
        with patch.object(U.P.Replay,'active',return_value=dict(wm_classes=['gedit','Gedit'],net_wm_name='note.txt')):
            with self.assertRaisesRegex(RuntimeError,'not a Nautilus'):
                replay.active('FILES')
        with patch.object(U.P.Replay,'active',return_value=dict(wm_classes=['org.gnome.Nautilus'],net_wm_name='4k')):
            self.assertEqual(replay.active('FILES')['net_wm_name'],'4k')

    def test_save_path_never_replaces_document_when_dialog_did_not_open(self):
        replay=object.__new__(U.Replay)
        replay.ensure=Mock();replay.active=Mock(return_value=dict(net_wm_name='word.docx - WPS Office'))
        replay.type=Mock();replay.input=Mock()
        with self.assertRaisesRegex(RuntimeError,'refusing to type'):
            replay.action(dict(target_app_key='WPS',operation='save_as_path',params={},group_index=1))
        replay.type.assert_not_called();replay.input.assert_not_called()

    def test_wps_save_as_clears_late_popups_and_retries_f12(self):
        replay=object.__new__(U.Replay)
        replay.ensure=Mock();replay.wps_ready=Mock();replay.key=Mock()
        replay.active=Mock(return_value=dict(net_wm_name='wps'))
        replay.wps_launch_mode='components'
        event=dict(target_app_key='WPS',operation='save_as_dialog',params={},group_index=1)
        with patch.object(U.time,'sleep'), patch.object(U.P,'wait_for',side_effect=[RuntimeError('blocked'), True]):
            replay.action(event)
        self.assertEqual(replay.wps_ready.call_count,3)
        replay.key.assert_any_call('WPS','Escape')
        self.assertEqual([call.args for call in replay.key.call_args_list].count(('WPS','F12')),2)

    def test_wps_initial_blank_writer_is_valid_component_editor(self):
        replay=object.__new__(U.Replay)
        replay.doc={'WPS':'word.docx'};replay.wps_launch_mode='components'
        replay.owned=Mock(return_value=True)
        replay.state=Mock(return_value=dict(windows=[dict(window_id='0xabc',is_normal_window=True,
            net_wm_name='WPS Writer - WPS Writer')]))
        with patch.object(U.P,'dismiss_wps_popups'), patch.object(U,'command') as command:
            self.assertEqual(replay.editor(),'0xabc')
        command.assert_called_with(['xdotool','windowactivate','--sync','0xabc'])


if __name__=='__main__':unittest.main()
