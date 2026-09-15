import sys,unittest
from pathlib import Path
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dataset.build import label
from models.segmented import LSTMSegmentedReentry,masked_ce,predict
from models.app_lstm_visit_explicit import AppLSTMVisitExplicit,explicit_features


class ContractTests(unittest.TestCase):
    def test_edges_and_censor(self):
        bins=[30,60,180,300,600,1800,3600]
        for k,edge in enumerate(bins):
            self.assertEqual(label(100,100+edge,10000,bins)[1],k+1)
            self.assertEqual(label(100,100+edge-.01,10000,bins)[1],k)
        self.assertEqual(label(100,None,700,bins)[1:],(-1,True,False))
        self.assertEqual(label(100,None,3700,bins)[1:],(7,True,True))
        self.assertEqual(label(100,100,120,bins)[1:],(-1,True,False))
        self.assertEqual(label(100,500,400,bins)[1:],(-1,True,False))

    def test_remaining_uses_query(self):
        a=label(10,100,200,[30,60,180]);b=label(40,100,200,[30,60,180])
        self.assertEqual((a[0],b[0]),(90,60))

    def test_encoder_unchanged_and_padding(self):
        torch.manual_seed(42);old=AppLSTMVisitExplicit(num_apps=32,num_user_groups=1,pad_id=30)
        torch.manual_seed(42);new=LSTMSegmentedReentry(num_apps=32,num_user_groups=1,pad_id=30)
        for k,v in old.state_dict().items():
            if not k.startswith(('output.','explicit_head.2.')):torch.testing.assert_close(v,new.state_dict()[k])
        new.eval()
        common={'opened_apps':torch.ones(1,32),'current_app':torch.tensor([1]),'time_feature':torch.zeros(1,3),'user_group':torch.tensor([0]),
            'explicit':torch.tensor(explicit_features([0,1],[0,20],25,32)[None])}
        def run(n):return new(history_apps=torch.tensor([[30]*n+[0,1]]),history_durations=torch.tensor([[0.]*n+[20.,5.]]),history_mask=torch.tensor([[0.]*n+[1.,1.]]),**common)
        x=run(18);torch.testing.assert_close(x,run(3));p,c,s=predict(x)
        self.assertEqual(tuple(p.shape),(1,32,8));torch.testing.assert_close(p.sum(-1),torch.ones(1,32))
        self.assertTrue(((s>=0)&(s<=7)).all())
        mask=torch.zeros(1,32,dtype=torch.bool);mask[0,0]=True
        y=torch.full((1,32),-1,dtype=torch.long);y[0,0]=3
        loss=masked_ce(x,y,mask);loss.backward();self.assertTrue(torch.isfinite(loss))
        self.assertTrue(all(torch.isfinite(v.grad).all() for v in new.parameters() if v.grad is not None))

    def test_extreme_logits_and_all_masked(self):
        x=torch.tensor([1000.,-1000.]*8).reshape(2,8).requires_grad_()
        loss=masked_ce(x,torch.tensor([1,-1]),torch.tensor([True,False]));loss.backward()
        self.assertTrue(torch.isfinite(loss));self.assertTrue(torch.isfinite(x.grad).all())
        self.assertEqual(masked_ce(x,torch.tensor([-1,-1]),torch.zeros(2,dtype=torch.bool)).item(),0)


if __name__=='__main__':unittest.main()
