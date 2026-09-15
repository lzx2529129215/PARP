import torch
from torch import nn
from common import *
from models.segmented import LSTMSegmentedReentry
class OrdinalReentry(LSTMSegmentedReentry):
 def __init__(self,thresholds_s=None,**kwargs):
  self.thresholds_s=tuple(thresholds_s or CFG['thresholds_s'])
  if any(a>=b for a,b in zip(self.thresholds_s,self.thresholds_s[1:])):raise ValueError('thresholds must increase')
  super().__init__(num_segments=len(self.thresholds_s),**kwargs)
 def forward(self,**features):
  z=super().forward(**features)
  # Ordered logits make S(t) non-increasing by construction, without post-hoc sorting.
  return torch.cat([z[...,:1],z[...,:1]-torch.cumsum(nn.functional.softplus(z[...,1:]),dim=-1)],dim=-1)
def episode_bce(logits,y,mask,weight):
 losses=nn.functional.binary_cross_entropy_with_logits(logits,y,reduction='none');n=mask.sum(-1);pair=(losses*mask).sum(-1)/n.clamp_min(1)
 return (pair*weight*(n>0)).sum()
