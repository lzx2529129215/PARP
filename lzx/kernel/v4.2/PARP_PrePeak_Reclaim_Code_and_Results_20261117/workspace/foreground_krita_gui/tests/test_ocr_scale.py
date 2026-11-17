"""Screenshot OCR scale/coordinate test; no real application is started."""
import io
import unittest
from unittest.mock import Mock,patch
from PIL import Image
from oomlab_krita_gui.visual_verifier import Visual,dimensions


class OcrScaleTests(unittest.TestCase):
    def test_localized_pixel_dimension_grouping(self):
        self.assertTrue(dimensions('4,096 x 4,096 (256 MiB)',4096,4096))
        self.assertFalse(dimensions('14,096 x 4,096',4096,4096))

    def test_scaled_ocr_boxes_return_to_screen_coordinates(self):
        text='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t20\t12\t74\t26\t95\tLayer\n'
        response=Mock(returncode=0,stdout=text.encode())
        with patch('oomlab_krita_gui.visual_verifier.subprocess.run',return_value=response) as run:
            words=Visual(dict(ocr_language='eng',ocr_confidence=55)).words(Image.new('RGB',(50,20)))
        with Image.open(io.BytesIO(run.call_args.kwargs['input'])) as sent:
            self.assertEqual(sent.size,(100,40))
        self.assertEqual({k:words[0][k] for k in ['x','y','w','h']},dict(x=10,y=6,w=37,h=13))
