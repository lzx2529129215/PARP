from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from oomlab_krita_gui.config import ROOT,load
from oomlab_krita_gui.asset_inspector import discover,pin,inspect
from oomlab_krita_gui.visual_verifier import zoom,dimensions,find_phrase,parse_tsv
from oomlab_krita_gui.window_manager import parse_windows,Windows


class VerificationTests(unittest.TestCase):
    def test_ocr_zoom_and_dimensions(self):
        self.assertEqual(zoom('Zoom 12.5%'),12.5)
        for text in ['no percent','10% 20%','0%']:
            with self.assertRaises(ValueError): zoom(text)
        self.assertTrue(dimensions('4096 × 8192 px',4096,8192))
        self.assertFalse(dimensions('14096 x 8192',4096,8192))

    def test_discard_phrase_does_not_match_save(self):
        words=[dict(text=t,x=i*30,y=0,w=25,h=12,line=('1','1','1')) for i,t in enumerate(["Don't",'Save','Cancel'])]
        match=find_phrase(words,["Don't Save"])
        self.assertEqual(len(match),1); self.assertEqual(match[0]['x'],0)
        self.assertEqual(find_phrase(words,['Discard']),[])

    def test_phrase_cannot_span_distant_buttons(self):
        words=[dict(text=t,x=x,y=0,w=25,h=12,line=('1','1','1')) for t,x in [("Don't",0),('Save',300)]]
        self.assertEqual(find_phrase(words,["Don't Save"]),[])

    def test_ocr_confidence_rejects_uncertain_words(self):
        t='level\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t0\t0\t40\t10\t20\tDiscard\n'
        self.assertEqual(parse_tsv(t,55),[])

    def test_window_parse_and_pid_mismatch(self):
        rows=parse_windows('0x040001 0 77 10 20 800 600 host Krita\n')
        self.assertEqual(rows[0]['pid'],77)
        with patch.object(Windows,'all',return_value=rows):
            with self.assertRaises(RuntimeError): Windows().verify('0x040001',99)

    def test_asset_discovery_never_substitutes_smoke_or_chooses_ambiguous(self):
        c=load(ROOT/'configs/gui.json')
        paths=[Path('/fixture/krita_stage4g.ora'),Path('/fixture/smoke.ora')]
        with patch('pathlib.Path.is_dir',return_value=True),patch('pathlib.Path.rglob',return_value=paths), \
             patch('oomlab_krita_gui.asset_inspector.inspect',side_effect=lambda p:dict(path=str(p),width=8192,height=8192,layers=4,sha256='a'*64,bytes=42)):
            rows=discover(c)
        self.assertEqual([r['eligible'] for r in rows],[True,False])
        with tempfile.TemporaryDirectory() as d:
            picked=pin(c,rows,Path(d)/'selected.json')
            self.assertEqual(picked['path'],str(paths[0]))
            with self.assertRaises(ValueError): pin(c,[rows[0],rows[0]],Path(d)/'bad.json')

    def test_real_ora_inspection_uses_only_mock_metadata_not_generated_asset(self):
        fake_path=Mock(suffix='.ora'); fake_path.is_file.return_value=True
        fake_path.stat.return_value.st_size=123
        fake_path.__str__=Mock(return_value='/existing/fixture.ora')
        fake=Mock(); fake.expanduser.return_value.resolve.return_value=fake_path
        archive=Mock(); archive.getinfo.return_value.file_size=100
        archive.read.side_effect=lambda n:b'image/openraster' if n=='mimetype' else b'<image w="4096" h="4096"><stack><layer name="one" src="data/a.png"/></stack></image>'
        archive.namelist.return_value=['data/a.png']
        ctx=Mock(); ctx.__enter__=Mock(return_value=archive);ctx.__exit__=Mock(return_value=False)
        with patch('oomlab_krita_gui.asset_inspector.Path',side_effect=lambda p:fake if str(p)=='/existing/fixture.ora' else Path(p)),patch('oomlab_krita_gui.asset_inspector.zipfile.ZipFile',return_value=ctx),patch('oomlab_krita_gui.asset_inspector.sha256',return_value='a'*64):
            result=inspect('/existing/fixture.ora')
            self.assertEqual((result['width'],result['height'],result['layers']),(4096,4096,1))
            self.assertEqual(result['sha256'],'a'*64)
