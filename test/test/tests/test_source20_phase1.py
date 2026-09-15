"""Reject false GUI acceptance from unchanged or lossy saved artifacts."""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from source20_phase1_gui import validate_config, validate_split, word_contains, timeline_entries


class PhaseOneTests(unittest.TestCase):
    def test_runtime_schema_and_vocab(self):
        scope = validate_config()
        self.assertEqual(len(scope['apps']), 30)
        self.assertFalse(any(a['prediction_enabled'] for a in scope['apps']))

    def test_split_requires_exact_coverage_and_fixed_cut(self):
        before = [dict(producer='old', **{'in': '00:00:00.000', 'out': '00:00:05.733'})]
        after = [dict(producer='a', **{'in': '00:00:00.000', 'out': '00:00:01.967'}),
                 dict(producer='b', **{'in': '00:00:02.000', 'out': '00:00:05.733'})]
        self.assertEqual(validate_split(before, after)['split_frames'], [[0, 59], [60, 172]])
        for wrong in ['00:00:02.033', '00:00:01.967']:
            broken = [after[0], dict(after[1], **{'in': wrong})]
            with self.assertRaises(ValueError):
                validate_split(before, broken)
        with self.assertRaises(ValueError):
            validate_split(before, before)

    def test_word_verification_reads_document_not_unrelated_zip_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.docx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', '<document><p>before</p></document>')
                archive.writestr('docProps/core.xml', '<props>EDIT_TOKEN</props>')
            self.assertFalse(word_contains(path, 'EDIT_TOKEN'))
            self.assertTrue(word_contains(path, 'before'))

    def test_playlist_and_background_do_not_count_as_video_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.mlt'
            path.write_text('<mlt><playlist id="background"><entry in="0" out="99"/></playlist>'
                            '<playlist id="source"><entry in="0" out="99"/></playlist>'
                            '<playlist id="video"><property name="shotcut:video">1</property>'
                            '<entry in="0" out="49"/><entry in="50" out="99"/></playlist></mlt>')
            self.assertEqual(len(timeline_entries(path)), 2)


if __name__ == '__main__':
    unittest.main()
