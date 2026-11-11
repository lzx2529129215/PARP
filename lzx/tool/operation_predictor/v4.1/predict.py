"""Standalone JSON-lines replay interface; no kernel/control side effects."""
import json,sys
from pathlib import Path
import torch
from train import encode
torch.set_num_threads(1)
model=torch.jit.load(str(Path(__file__).resolve().parent/'model/rankprop_v4_1.ts')).eval()
history=[]
for line in sys.stdin:
 event=json.loads(line)
 if event.get('reset'):history=[];continue
 history.append(dict(current_app=event['app'],timestamp=event['timestamp']))
 x,candidates=encode(history,len(history)-1)
 if not candidates:print(json.dumps({'eviction_order':[]}));continue
 with torch.inference_mode():
  logits,score,expected=model(*(torch.from_numpy(v.copy()).unsqueeze(0) for v in x))
  order=sorted(range(len(candidates)),key=lambda i:(-float(score[0,i]),i))
  result=[dict(app=candidates[i],eviction_score=float(score[0,i]),expected_distance=float(expected[0,i]*17),p_no_return=float(logits[0,i].softmax(-1)[-1])) for i in order]
 print(json.dumps({'eviction_order':result}),flush=True)
