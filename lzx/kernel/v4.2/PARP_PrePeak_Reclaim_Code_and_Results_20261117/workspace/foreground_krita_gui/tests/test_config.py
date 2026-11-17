import copy
import unittest
from oomlab_krita_gui.config import ROOT,load,validate


class ConfigTests(unittest.TestCase):
    def test_default_requires_asset_pin(self):
        c=load(ROOT/'configs/gui.json'); self.assertIsNone(c['asset']['path'])
        self.assertNotIn('memory_max',c)

    def test_invalid_time_region_and_asset(self):
        for change in [dict(poll_s=float('nan')),dict(asset=dict(path='/x',sha256=None)),dict(zoom_scroll_ticks=0),dict(extra=1)]:
            with self.subTest(change=change),self.assertRaises(ValueError): validate(dict(load(ROOT/'configs/gui.json'),**change))
        c=load(ROOT/'configs/gui.json'); c['regions']['canvas']=[0,0,2,1]
        with self.assertRaises(ValueError): validate(c)
