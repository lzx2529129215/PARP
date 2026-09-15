import datetime as dt
import json
from pathlib import Path
import sys
import unittest

import torch

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'test/test'),str(ROOT/'lzx/tool/operation_predictor')]
from audit_lsapp_30_mapping import load_map, project_opened
from v3.src.data.build_app_dataset_duration import build_segments
from v3.src.data.build_app_dataset_visit_window import window_labels
from v3.models.app_lstm_visit_window import visit_probabilities, masked_loss


class MappingTrainingTests(unittest.TestCase):
    def setUp(self):
        pred=ROOT/'lzx/tool/operation_predictor'
        self.mapping=load_map(pred/'data/lsapp_30/mapping/lsapp_to_linux.json')
        self.vocab=json.loads((pred/'data/vocab/lsapp_30/app_vocab_duration.json').read_text())

    def test_alias_close_and_unknown_boundary(self):
        self.assertEqual(project_opened({'Samsung Internet Browser'},self.mapping),{'Falkon'})
        raw=['Google Chrome','Phone','Google Chrome']
        rows=[{'user_id':'u','timestamp':f'2020-01-01 00:00:{i*10:02}',
               'foreground_app':self.mapping.get(a,'<UNKNOWN>'),'raw_foreground_app':a,
               'opened_apps':'Falkon','user_group':'通用用户'} for i,a in enumerate(raw)]
        segments,_=build_segments(rows,3600)
        self.assertEqual([s['app'] for s in segments],['Falkon','<UNKNOWN>','Falkon'])

    def test_windows_censoring_and_special_tokens(self):
        t=dt.datetime(2020,1,1)
        entries=[(t,'Falkon'),(t+dt.timedelta(seconds=30),'Dino'),
                 (t+dt.timedelta(seconds=180),'Gajim')]
        y,m=window_labels(entries,t,t+dt.timedelta(seconds=180),self.vocab)
        self.assertEqual(y[30],['Dino']);self.assertEqual(set(y[180]),{'Dino','Gajim'})
        y,m=window_labels(entries,t,t+dt.timedelta(seconds=40),self.vocab)
        self.assertEqual(m[180][self.vocab['Dino']],1)
        self.assertEqual(m[180][self.vocab['Gajim']],0)
        self.assertEqual(m[30][self.vocab['<UNKNOWN>']],0)
        self.assertEqual(m[180][self.vocab['<PAD>']],0)

    def test_32_output_extremes_are_finite_and_nested(self):
        z=torch.tensor([-1000.,1000.,0.,-1000.]*32).reshape(2,32,2).requires_grad_()
        p=visit_probabilities(z)
        self.assertTrue((p[:,:,0]<=p[:,:,1]).all())
        y=torch.zeros_like(z);y[0,:,0]=1
        mask=torch.ones_like(z);mask[:,30:]=0
        loss=masked_loss(z,y,mask);loss.backward()
        self.assertTrue(torch.isfinite(loss));self.assertTrue(torch.isfinite(z.grad).all())


if __name__=='__main__':unittest.main()
