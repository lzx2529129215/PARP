"""Pure offline layer. No model class, runtime sink, or kernel import."""
import numpy as np

def risk_coldness(survival,thresholds):
 s=np.asarray(survival,float);t=np.asarray(thresholds,float)
 if s.shape[-1]!=len(t) or np.any(np.diff(t)<=0) or np.any(np.diff(s,axis=-1)>1e-6) or not np.isfinite(s).all() or np.any((s<0)|(s>1)):raise ValueError('invalid survival curve')
 if 30 not in t or 180 not in t:raise ValueError('diagnostic risks require 30s and 180s thresholds')
 knots=np.concatenate([np.ones(s.shape[:-1]+(1,)),s],axis=-1);dt=np.diff(np.r_[0,t]);cold=((knots[...,:-1]+knots[...,1:])*.5*dt).sum(-1)/t[-1]
 q=np.clip(s,1e-7,1-1e-7);entropy=-(q*np.log(q)+(1-q)*np.log(1-q)).mean(-1)/np.log(2)
 return dict(risk30=1-s[...,np.where(t==30)[0][0]],risk180=1-s[...,np.where(t==180)[0][0]],coldness=cold,uncertainty=entropy,survival180=s[...,np.where(t==180)[0][0]])

def ranks_to_bins(values,lo,hi):
 unique=np.unique(values)
 if len(unique)<=1:return np.full(len(values),lo,dtype=np.int8)
 r=np.searchsorted(unique,values);return (lo+np.floor(r*(hi-lo)/(len(unique)-1))).astype(np.int8)

def map_priority(coldness,eligible,risk30=None,risk180=None,kind='RankOnly',num_bins=8,hot30=.5,hot180=.8):
 if num_bins<3:raise ValueError('at least three priority levels')
 out=np.full(np.shape(coldness),-1,dtype=np.int8)
 for i in range(len(out)):
  ids=np.flatnonzero(eligible[i]);v=np.asarray(coldness[i])[ids]
  if not len(ids):continue
  if kind=='RankOnly':out[i,ids]=ranks_to_bins(v,0,num_bins-1)
  elif kind=='RiskAwareRank':
   hot=(risk30[i,ids]>=hot30)|(risk180[i,ids]>=hot180)
   out[i,ids[hot]]=ranks_to_bins(v[hot],0,1);out[i,ids[~hot]]=ranks_to_bins(v[~hot],2,num_bins-1)
  else:raise ValueError(kind)
 return out
