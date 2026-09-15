"""Existing duration/explicit encoders; replace only terminal output layers."""
import torch
from torch import nn
from .app_lstm_visit_explicit import AppLSTMVisitExplicit
from .app_lstm_visit_window import AppLSTMVisitWindow

MODEL_TYPE='lstm_segmented_reentry_v1'


class LSTMSegmentedReentry(AppLSTMVisitExplicit):
    def __init__(self,num_segments=8,**kwargs):
        super().__init__(**kwargs)
        if num_segments<2:raise ValueError('At least two segments required')
        self.num_segments=num_segments
        self.output=nn.Linear(self.output.in_features,self.num_apps*num_segments)
        self.explicit_head[-1]=nn.Linear(self.explicit_head[-1].in_features,self.num_apps*num_segments)

    def forward(self,explicit,**features):
        # Calling the unchanged v3 encoder via its inherited forward avoids a
        # copied/rewritten LSTM path. Identity output exposes the shared vector.
        apps,durations,mask=(features[k] for k in ('history_apps','history_durations','history_mask'))
        positions=torch.arange(apps.shape[1],device=apps.device)[None,:]
        order=torch.argsort(positions+(1-mask.long())*apps.shape[1],dim=1)
        compact={**features,**{k:v.gather(1,order) for k,v in zip(('history_apps','history_durations','history_mask'),(apps,durations,mask))}}
        from .app_lstm_duration import AppLSTMNextV3
        logits=AppLSTMNextV3.forward(self,**compact).reshape(-1,self.num_apps,self.num_segments)
        return logits+self.explicit_head(explicit.flatten(1)).reshape(-1,self.num_apps,self.num_segments)


def masked_ce(logits,labels,valid):
    if valid.any():return nn.functional.cross_entropy(logits[valid],labels[valid])
    return logits.sum()*0


def predict(logits):
    p=logits.softmax(-1)
    score=(p*torch.arange(p.shape[-1],device=p.device,dtype=p.dtype)).sum(-1)
    return p,p.argmax(-1),score
