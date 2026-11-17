import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from oomlab_krita_gui.config import ROOT,load
from oomlab_krita_gui.gui_controller import Controller
from oomlab_krita_gui.process_manager import Process
from oomlab_krita_gui.window_manager import Windows


class GuiLogicTests(unittest.TestCase):
    def test_popup_uses_visible_menu_owned_by_the_target_pid(self):
        windows=Windows()
        tree='     0x1 "Krita": () 100x80+1+2\n     0x2 "Other": () 100x80+1+2\n     0x3 "Krita": () 100x80+1+2\n'
        geometry='Absolute upper-left X: 190\nAbsolute upper-left Y: 37\nWidth: 376\nHeight: 410\nMap State: IsViewable\n'
        def command(args):
            if args[:3]==['xwininfo','-root','-tree']: return tree
            if args[0]=='xprop':
                pid=999 if args[2]=='0x2' else 123
                kind='NORMAL' if args[2]=='0x3' else 'POPUP_MENU'
                return f'_NET_WM_PID(CARDINAL) = {pid}\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_{kind}\n'
            return geometry
        with patch.object(windows,'verify'),patch.object(windows,'assert_active'), \
             patch('oomlab_krita_gui.window_manager.command',side_effect=command):
            self.assertEqual(windows.popup_boxes('0x0',123),[dict(id='0x1',x=190,y=37,width=376,height=410)])

    def test_modal_geometry_uses_actual_client_instead_of_gravity_offsets(self):
        windows=Windows()
        geometry='Absolute upper-left X: 540\nAbsolute upper-left Y: 331\nWidth: 500\nHeight: 122\nMap State: IsViewable\n'
        with patch.object(windows,'assert_active'), \
             patch('oomlab_krita_gui.window_manager.command',side_effect=['1234',geometry]):
            self.assertEqual(windows.active_box('0x1',123),dict(x=540,y=331,width=500,height=122))

    def test_window_selection_ignores_splash_with_same_pid_and_title(self):
        windows=Windows()
        splash=dict(id='0x1',pid=123,title='Krita')
        main=dict(id='0x2',pid=123,title='Krita')
        with patch.object(windows,'all',return_value=[splash,main]), \
             patch.object(windows,'verify',return_value=main), \
             patch('oomlab_krita_gui.window_manager.command',side_effect=lambda a:
                   '_NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_SPLASH, _NET_WM_WINDOW_TYPE_NORMAL' if a[2]=='0x1'
                   else '_NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_NORMAL'):
            self.assertEqual(windows.select(123,set())['id'],'0x2')

    def test_launch_needs_main_menu_before_accepting_window(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d))
            c.process.launch.return_value=dict(pid=123)
            c.windows.select.return_value=dict(id='0x2')
            c.anchor=Mock(side_effect=RuntimeError('splash has no Layer menu'))
            c.wait=lambda probe,kind='action':probe()
            with self.assertRaises(RuntimeError): c.launch_krita()
            c.mouse.hotkey.assert_not_called()

    def test_client_geometry_overrides_window_manager_gravity_offsets(self):
        windows=Windows()
        row=dict(id='0x2',pid=123,title='Krita',x=288,y=159,width=1144,height=568)
        info='Absolute upper-left X: 287\nAbsolute upper-left Y: 143\nWidth: 1144\nHeight: 568\nMap State: IsViewable\n'
        with patch.object(windows,'all',return_value=[row]), \
             patch('oomlab_krita_gui.window_manager.command',side_effect=lambda a:
                   '_NET_WM_PID = 123' if a[0]=='xprop' else info):
            actual=windows.verify('0x2',123)
        self.assertEqual((actual['x'],actual['y']),(287,143))

    def test_open_does_not_type_into_main_window_before_dialog(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); c.wid='0x1'; c.key=Mock()
            def wait(probe,kind='action'):
                self.assertFalse(probe()['ok']); raise TimeoutError('dialog not open')
            c.wait=wait
            with patch('oomlab_krita_gui.gui_controller.command',return_value='1') as command:
                with self.assertRaises(TimeoutError): c.open_ora()
            self.assertEqual(command.call_args.args[0],['xdotool','getactivewindow'])
            c.key.assert_called_once_with('ctrl','o')

    def test_added_layer_must_be_new_selected_row(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); image=Mock(); c.panel_before=image
            def row(number,y):
                return [dict(text=t,x=i*40,y=y,w=35,h=10,line=('1','1',str(y))) for i,t in enumerate(['Paint','Layer',str(number)])]
            c.old_layer_words=row(1,0); words=row(1,0)+row(2,30)
            c.visual.words.return_value=words
            c.visual.selected.side_effect=lambda image,box:dict(selected=box['y']==30)
            c.visual.changed.return_value=dict(changed=True,fraction=.1)
            c.region=Mock(return_value=(image,(0,0,200,100)))
            c.windows.verify.return_value=dict(title='asset * Krita')
            c.wait=lambda probe,kind='action':probe()
            result=c.verify_layer()
            self.assertTrue(result['ok']);self.assertEqual(result['new_layer_name'],'Paint Layer 2')

    def controller(self,run):
        mouse=Mock(); windows=Mock(); windows.all.return_value=[]
        c=Controller(load(ROOT/'configs/gui.json'),run,dict(path='/existing/krita_stage4g.ora',sha256='a'*64,layers=4),
                     mouse=mouse,windows=windows,visual=Mock(),process=Mock())
        c.guard=Mock(); return c

    def test_zoom_uses_real_scroll_input(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); c.region=Mock(return_value=(Mock(),(10,20,400,300)))
            c.visual.text.return_value='10%'
            c.zoom_in(); c.mouse.scroll.assert_called_with(2)
            c.zoom_out(); c.mouse.scroll.assert_called_with(-2)

    def test_create_layer_uses_mouse_menu_not_api(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); c.region=Mock(return_value=(Mock(),(0,0,100,100)))
            c.anchor=Mock(return_value=dict(ok=True)); c.wait=lambda f,kind='action':f()
            c.create_layer()
            self.assertEqual(len([call for call in c.anchor.call_args_list if call.kwargs.get('click')]),3)

    def test_discard_is_positive_mouse_target(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); c.key=Mock(); c.wait=Mock(return_value=dict(ok=True))
            c.anchor=Mock(return_value=dict(ok=True)); c.capture=Mock(return_value='prompt.png')
            c.close_document()
            c.key.assert_called_once_with('ctrl','w')
            self.assertEqual(c.anchor.call_args.kwargs,dict(modal=True,click=True))
            self.assertIn('Discard',c.anchor.call_args.args[0])

    def test_capture_writes_file_and_fails_if_save_did_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.controller(Path(d)); image=Mock()
            from PIL import Image
            image.save.side_effect=lambda p:Image.new('RGB',(16,16),'blue').save(p)
            c.mouse.screenshot.return_value=image
            self.assertTrue((Path(d)/c.capture('test.png')).is_file())
            image.save.side_effect=None
            with self.assertRaises(RuntimeError): c.capture('missing.png')

    def test_nonzero_exit_rejected_without_killing_process(self):
        with tempfile.TemporaryDirectory() as d:
            p=Process(Path(d)); p.child=Mock();p.child.poll.return_value=-11
            with self.assertRaises(RuntimeError): p.finish()
            p.child.terminate.assert_not_called();p.child.kill.assert_not_called()
