"""Ranking metrics with censor-aware comparability and explicit denominators."""
import itertools
import numpy as np


def direction(a,b,remaining,censored,observed):
    """Return sign(Ta-Tb) only when proven, including one right-censored time."""
    ca,cb=censored[a],censored[b]
    if not ca and not cb:
        d=remaining[a]-remaining[b]
        return int(np.sign(d)) if d else None
    if ca and not cb and remaining[b]<=observed:return 1
    if cb and not ca and remaining[a]<=observed:return -1
    return None


def earliest(selected,remaining,censored,observed):
    known=[remaining[i] for i in selected if not censored[i]]
    if not known:return np.nan
    value=min(known)
    return value if not any(censored[i] for i in selected) or value<=observed else np.nan


def query_metrics(candidates,scores,remaining,censored,observed):
    pairs=[(a,b,direction(a,b,remaining,censored,observed)) for a,b in itertools.combinations(candidates,2)]
    pairs=[x for x in pairs if x[2] is not None]
    result=[]
    for score in scores:
        ranked=sorted(candidates,key=lambda i:(-float(score[i]),i));victim=ranked[0]
        correct=sum(int(np.sign(score[a]-score[b]))==d for a,b,d in pairs)
        ties=sum(score[a]==score[b] for a,b,d in pairs)
        values={'poa_numerator':correct,'poa_denominator':len(pairs),'score_tied_pairs':ties,'victim':victim,
            'victim_time':remaining[victim] if not censored[victim] else np.nan,'victim_censored':bool(censored[victim])}
        for w in (30,180):
            known=not censored[victim] or observed>=w
            values[f'dvr{w}']=float(not censored[victim] and remaining[victim]<w) if known else np.nan
        for k in (1,2,3):
            value=np.nan
            # All candidates share an observation endpoint: censored ones are
            # later than every observed return. Their internal order is unknown,
            # but the top-K SET is identifiable if all censored ones fit in K.
            unknown=[i for i in candidates if censored[i]]
            if len(candidates)>k and len(unknown)<=k:
                actual=unknown+sorted((i for i in candidates if not censored[i]),key=lambda i:(-remaining[i],i))
                if k==len(unknown) or remaining[actual[k-1]]!=remaining[actual[k]]:
                    value=len(set(ranked[:k])&set(actual[:k]))/k
            values[f'cp{k}']=value
        for k in (2,4):values[f'evr{k}']=earliest(ranked[:k],remaining,censored,observed) if len(candidates)>=k else np.nan
        result.append(values)
    return result


def stats(values):
    a=np.asarray(values,float);a=a[np.isfinite(a)]
    return {'n':len(a),'mean':float(a.mean()) if len(a) else None,**{name:float(np.percentile(a,q)) if len(a) else None for name,q in [('p25',25),('median',50),('p75',75),('p90',90)]}}


def bootstrap_ratio(num,den,user,replicates=1000,seed=42):
    """Paired B2-B1 difference, resampling users (all their queries stay together)."""
    users,inv=np.unique(user,return_inverse=True);N=np.zeros((len(users),2));D=np.zeros_like(N)
    for m in range(2):np.add.at(N[:,m],inv,num[:,m]);np.add.at(D[:,m],inv,den[:,m])
    rng=np.random.default_rng(seed);deltas=[]
    for _ in range(replicates):
        ix=rng.integers(0,len(users),len(users));n=N[ix].sum(0);d=D[ix].sum(0)
        if (d>0).all():deltas.append(n[1]/d[1]-n[0]/d[0])
    return {'users':len(users),'ci95':np.quantile(deltas,[.025,.975]).tolist() if deltas else [None,None]}


def bootstrap_median_difference(values,user,replicates=1000,seed=42):
    users,inv=np.unique(user,return_inverse=True)
    orders=[np.argsort(values[:,m]) for m in range(2)]
    sorted_values=[values[orders[m],m] for m in range(2)]
    sorted_users=[inv[orders[m]] for m in range(2)]
    rng=np.random.default_rng(seed);samples=[]
    for _ in range(replicates):
        weights=np.bincount(rng.integers(0,len(users),len(users)),minlength=len(users));medians=[]
        for m in range(2):
            cw=np.cumsum(weights[sorted_users[m]]);total=int(cw[-1])
            lo=np.searchsorted(cw,(total+1)//2);hi=np.searchsorted(cw,(total+2)//2)
            medians.append((sorted_values[m][lo]+sorted_values[m][hi])/2)
        samples.append(medians[1]-medians[0])
    return {'users':len(users),'median_difference':float(np.median(values[:,1])-np.median(values[:,0])),
        'ci95':np.quantile(samples,[.025,.975]).tolist()}
