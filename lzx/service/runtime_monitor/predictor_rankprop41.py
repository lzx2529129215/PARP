"""RankProp 4.1 service adapter: separate return probabilities and reclaim rank."""
from pathlib import Path
from datetime import datetime
import csv,json,math
import torch

class OnlineRankProp41Predictor:
    def __init__(self,checkpoint,app_vocab,group_vocab,user_group,top_k=5,device_name='cpu'):
        torch.set_num_threads(1)
        self.device=torch.device('cpu');self.top_k=top_k
        self.model=torch.jit.load(str(checkpoint),map_location='cpu').eval()
        self.vocab=json.loads(Path(app_vocab).read_text())
        self.history=[];self.last={};self.count={};self.sequence=0
        self.audit=None
    def close(self):
        if self.audit:self.audit.close();self.audit=None
    @torch.inference_mode()
    def predict_bundle(self,history_apps,history_durations,history_mask,opened_apps,current_app,timestamp):
        if current_app not in self.vocab or self.vocab[current_app]>=15:
            return {'top_k_outputs':[],'all_probabilities':[],'probability_source':'unsupported_foreground','prediction_format':'app_probability'}
        stamp=datetime.fromisoformat(str(timestamp).replace('Z','+00:00'))
        if not self.history or self.history[-1][0]!=current_app:
            self.history.append((current_app,stamp,self.sequence));self.history=self.history[-32:]
            self.last[current_app]=self.sequence;self.count[current_app]=self.count.get(current_app,0)+1;self.sequence+=1
        candidates=sorted({a for a in opened_apps if a in self.vocab and self.vocab[a]<15 and a!=current_app},key=lambda a:(self.last.get(a,-1),a))
        if not candidates:return {'top_k_outputs':[],'all_probabilities':[],'probability_source':'rankprop41_distance_head','prediction_format':'app_probability'}
        ids=torch.full((1,32),15,dtype=torch.long);num=torch.zeros((1,32,2));mask=torch.zeros((1,32),dtype=torch.bool)
        for j,(a,t,seq) in enumerate(self.history):
            idx=32-len(self.history)+j;ids[0,idx]=self.vocab[a];mask[0,idx]=True
            num[0,idx,0]=(t.hour*3600+t.minute*60+t.second)/86400
            if j:num[0,idx,1]=min(1.,math.log1p(max(0,(t-self.history[j-1][1]).total_seconds()))/math.log1p(7*86400))
        cids=torch.full((1,15),15,dtype=torch.long);feat=torch.zeros((1,15,4));cmask=torch.zeros((1,15),dtype=torch.bool)
        for j,a in enumerate(candidates):
            last=self.last.get(a,-1);distinct=len({b for b,s in self.last.items() if s>last})
            count=self.count.get(a,0);age=self.sequence-1-last
            cids[0,j]=self.vocab[a];cmask[0,j]=True
            feat[0,j]=torch.tensor([1-j/max(1,len(candidates)-1),min(16,distinct)/16,min(32,age)/32,count/(count+10)])
        logits,scores,expected=self.model(ids,num,mask,cids,feat,cmask)
        # Conditional next-app distribution for demand consumers; rank is separate.
        mass=logits[0,:len(candidates)].softmax(-1)[:,0];probs=mass/mass.sum().clamp(min=1e-12)
        protection_order=sorted(range(len(candidates)),key=lambda j:(float(scores[0,j]),j))
        ranks={j:r+1 for r,j in enumerate(protection_order)};rows=[]
        for j in protection_order:
            p=float(probs[j]);rows.append(dict(app=candidates[j],app_id=self.vocab[candidates[j]],rank=ranks[j],probability=p,next_use_probability=p,next_use_probability_fixed=round(p*10000),probability_source='rankprop41_conditional_distance1',score_mode='distance_head',prediction_format='app_probability',reclaim_priority_rank=ranks[j],reclaim_rank_only=True,eviction_score=float(scores[0,j]),expected_distance=float(expected[0,j]*17),p_no_return=float(logits[0,j].softmax(-1)[-1])))
        if self.audit:
            self.audit.write(json.dumps({'timestamp':timestamp,'foreground':current_app,'history_length':len(self.history),'rows':rows})+'\n');self.audit.flush()
        return {'top_k_outputs':rows[:self.top_k],'all_probabilities':rows,'probability_source':'rankprop41_conditional_distance1','prediction_format':'app_probability'}
