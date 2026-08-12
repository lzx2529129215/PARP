"""Dependency-free deterministic models and metrics for Phase2.8B."""

from collections import Counter, defaultdict
import math
import random
import time


def softmax(values):
    peak=max(values); values=[math.exp(max(-50,min(50,x-peak))) for x in values]; total=sum(values)
    return [x/total for x in values]


class Standardizer:
    def fit(self, rows):
        self.mean=[sum(c)/len(c) for c in zip(*rows)]
        self.scale=[(sum((v-m)**2 for v in c)/len(c))**.5 or 1.0 for c,m in zip(zip(*rows),self.mean)]
        self.fit_scope="TRAIN_ONLY"; return self
    def transform(self, rows): return [[(v-m)/s for v,m,s in zip(r,self.mean,self.scale)] for r in rows]
    def export(self): return {"mean":self.mean,"scale":self.scale,"fit_scope":self.fit_scope}


def select_features(rows, labels, limit=96):
    classes=sorted(set(labels)); overall=[sum(c)/len(c) for c in zip(*rows)]; scores=[]
    for j in range(len(overall)):
        between=0; within=0
        for c in classes:
            values=[r[j] for r,y in zip(rows,labels) if y==c]
            if not values: continue
            mean=sum(values)/len(values); between+=len(values)*(mean-overall[j])**2
            within+=sum((x-mean)**2 for x in values)
        scores.append((between/(within+1e-9),j))
    return [j for _,j in sorted(scores,key=lambda x:(-x[0],x[1]))[:limit]]


class Centroid:
    def fit(self,rows,labels):
        self.classes=sorted(set(labels)); groups=defaultdict(list)
        for r,y in zip(rows,labels): groups[y].append(r)
        self.centroids={c:[sum(x)/len(x) for x in zip(*groups[c])] for c in self.classes}; return self
    def probabilities(self,rows):
        out=[]
        for r in rows:
            dist=[sum((a-b)**2 for a,b in zip(r,self.centroids[c]))**.5 for c in self.classes]
            out.append(softmax([-x/max(1,len(r)**.5) for x in dist]))
        return out
    def export(self): return {"type":"Centroid","classes":self.classes,"centroids":self.centroids}


class SoftmaxRegression:
    def __init__(self,epochs=24,rate=.04,l2=1e-4): self.epochs=epochs; self.rate=rate; self.l2=l2
    def fit(self,rows,labels):
        self.classes=sorted(set(labels)); index={c:i for i,c in enumerate(self.classes)}; d=len(rows[0]); k=len(self.classes)
        self.weights=[[0.0]*(d+1) for _ in range(k)]
        for epoch in range(self.epochs):
            rate=self.rate/(1+epoch*.08)
            for r,y in zip(rows,labels):
                x=r+[1.0]; probs=softmax([sum(a*b for a,b in zip(w,x)) for w in self.weights]); yi=index[y]
                for c in range(k):
                    error=(1.0 if c==yi else 0.0)-probs[c]
                    for j,v in enumerate(x): self.weights[c][j]+=rate*(error*v-self.l2*self.weights[c][j])
        return self
    def probabilities(self,rows): return [softmax([sum(a*b for a,b in zip(w,r+[1.0])) for w in self.weights]) for r in rows]
    def export(self): return {"type":"LogisticRegression","classes":self.classes,"weights":self.weights}


class SGDLinear:
    def __init__(self,epochs=18,rate=.03): self.epochs=epochs; self.rate=rate
    def fit(self,rows,labels):
        self.classes=sorted(set(labels)); d=len(rows[0]); self.weights=[[0.0]*(d+1) for _ in self.classes]
        for epoch in range(self.epochs):
            rate=self.rate/(1+epoch*.1)
            for r,y in zip(rows,labels):
                x=r+[1.0]
                for ci,c in enumerate(self.classes):
                    target=1 if y==c else -1; margin=target*sum(a*b for a,b in zip(self.weights[ci],x))
                    if margin<1:
                        for j,v in enumerate(x): self.weights[ci][j]+=rate*target*v
        return self
    def probabilities(self,rows): return [softmax([sum(a*b for a,b in zip(w,r+[1.0])) for w in self.weights]) for r in rows]
    def export(self): return {"type":"SGDClassifier","classes":self.classes,"weights":self.weights}


class RandomForestLite:
    """Deterministic bootstrapped decision-stump forest."""
    def __init__(self,trees=96,seed=28): self.trees=trees; self.seed=seed
    def fit(self,rows,labels):
        self.classes=sorted(set(labels)); rng=random.Random(self.seed); n=len(rows); d=len(rows[0]); self.forest=[]
        for _ in range(self.trees):
            j=rng.randrange(d); sample=[rng.randrange(n) for _ in range(n)]; values=sorted(rows[i][j] for i in sample); threshold=values[len(values)//2]
            left=Counter(labels[i] for i in sample if rows[i][j]<=threshold); right=Counter(labels[i] for i in sample if rows[i][j]>threshold)
            self.forest.append((j,threshold,[left[c]+1 for c in self.classes],[right[c]+1 for c in self.classes]))
        return self
    def probabilities(self,rows):
        output=[]
        for r in rows:
            votes=[0.0]*len(self.classes)
            for j,t,left,right in self.forest:
                counts=left if r[j]<=t else right; total=sum(counts)
                for c,x in enumerate(counts): votes[c]+=x/total
            output.append([x/self.trees for x in votes])
        return output
    def export(self): return {"type":"RandomForest","classes":self.classes,"trees":self.forest}


class HistGradientBoostingLite:
    """Additive class-wise residual stumps over train-derived quartiles."""
    def __init__(self,rounds=20,rate=.15): self.rounds=rounds; self.rate=rate
    def fit(self,rows,labels):
        self.classes=sorted(set(labels)); d=len(rows[0]); n=len(rows); scores=[[0.0]*len(self.classes) for _ in rows]; self.stumps=[]
        candidate=list(range(min(d,64)))
        for _ in range(self.rounds):
            probs=[softmax(x) for x in scores]
            for ci,c in enumerate(self.classes):
                residual=[(1 if y==c else 0)-p[ci] for y,p in zip(labels,probs)]; best=None
                for j in candidate:
                    vals=sorted(r[j] for r in rows)
                    for t in (vals[n//4],vals[n//2],vals[3*n//4]):
                        li=[i for i,r in enumerate(rows) if r[j]<=t]; ri=[i for i,r in enumerate(rows) if r[j]>t]
                        if not li or not ri: continue
                        lv=sum(residual[i] for i in li)/len(li); rv=sum(residual[i] for i in ri)/len(ri)
                        loss=(sum((residual[i]-lv)**2 for i in li)+
                              sum((residual[i]-rv)**2 for i in ri))
                        if best is None or loss<best[0]: best=(loss,j,t,lv,rv)
                if best:
                    _,j,t,lv,rv=best; self.stumps.append((ci,j,t,lv,rv))
                    for i,r in enumerate(rows): scores[i][ci]+=self.rate*(lv if r[j]<=t else rv)
        return self
    def probabilities(self,rows):
        output=[]
        for r in rows:
            scores=[0.0]*len(self.classes)
            for ci,j,t,lv,rv in self.stumps: scores[ci]+=self.rate*(lv if r[j]<=t else rv)
            output.append(softmax(scores))
        return output
    def export(self): return {"type":"HistGradientBoosting","classes":self.classes,"rate":self.rate,"stumps":self.stumps}


def metrics(labels, probabilities, classes, rejected=None):
    rejected=rejected or [False]*len(labels); confusion={c:Counter() for c in classes}; correct=0; top2=top3=0; brier=0
    for y,p,reject in zip(labels,probabilities,rejected):
        order=sorted(range(len(classes)),key=lambda i:(-p[i],classes[i])); pred="UNKNOWN" if reject else classes[order[0]]
        confusion[y][pred]+=1; correct+=pred==y; top2+=y in [classes[i] for i in order[:2]]; top3+=y in [classes[i] for i in order[:3]]
        brier+=sum((p[i]-(1 if classes[i]==y else 0))**2 for i in range(len(classes)))
    precisions=[]; recalls=[]; f1s=[]; weighted=0
    per={}
    for c in classes:
        tp=confusion[c][c]; fp=sum(confusion[x][c] for x in classes if x!=c); fn=sum(confusion[c].values())-tp
        pr=tp/(tp+fp) if tp+fp else 0; re=tp/(tp+fn) if tp+fn else 0; f=2*pr*re/(pr+re) if pr+re else 0
        support=sum(confusion[c].values()); precisions.append(pr); recalls.append(re); f1s.append(f); weighted+=f*support
        per[c]={"precision":pr,"recall":re,"f1":f,"support":support}
    n=max(1,len(labels)); covered=max(1,len(labels)-sum(rejected))
    return {"samples":len(labels),"accuracy":correct/n,"balanced_accuracy":sum(recalls)/max(1,len(classes)),
            "macro_precision":sum(precisions)/max(1,len(classes)),"macro_recall":sum(recalls)/max(1,len(classes)),
            "macro_f1":sum(f1s)/max(1,len(classes)),"weighted_f1":weighted/n,"top2_accuracy":top2/n,"top3_accuracy":top3/n,
            "unknown_rate":sum(rejected)/n,"coverage":1-sum(rejected)/n,"covered_accuracy":correct/covered,
            "brier_score":brier/n,"per_class":per,"confusion":{k:dict(v) for k,v in confusion.items()}}


def binary_metrics(labels, probabilities, threshold=.5):
    tp=fp=tn=fn=0
    for y,p in zip(labels,probabilities):
        pred=p>=threshold
        if y and pred: tp+=1
        elif y: fn+=1
        elif pred: fp+=1
        else: tn+=1
    precision=tp/(tp+fp) if tp+fp else 0; recall=tp/(tp+fn) if tp+fn else 0
    return {"samples":len(labels),"threshold":threshold,"precision":precision,"recall":recall,
            "f1":2*precision*recall/(precision+recall) if precision+recall else 0,
            "false_cold":fn/(tp+fn) if tp+fn else 0,"false_hot":fp/(tn+fp) if tn+fp else 0,
            "positive_rate":(tp+fn)/max(1,len(labels)),"predicted_hot_rate":(tp+fp)/max(1,len(labels))}
