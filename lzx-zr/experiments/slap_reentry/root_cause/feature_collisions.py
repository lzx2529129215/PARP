from common import *
from collections import defaultdict
from scripts.train import FEATURES

def main():
    d=load_data(BASE/'dataset/test');groups=defaultdict(lambda:np.zeros(8,np.int64));p=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r');representative={};max_p_delta=0.
    for qi in np.flatnonzero(d['label_valid'].any(1)):
        # Equal capped duration implies equal model normalization. Include ALL
        # other feature tensors, not merely candidate recency. User/session are
        # grouping restrictions, not new features supplied to the model.
        digest=hashlib.sha256()
        for key in FEATURES:
            x=d[key][qi]
            if key=='history_durations':x=np.clip(x,0,600)
            digest.update(np.asarray(x).tobytes())
        for a in np.flatnonzero(d['label_valid'][qi]):
            k=(int(d['user'][qi]),int(d['session'][qi]),int(a),digest.digest());groups[k][d['labels'][qi,a]]+=1
            if k in representative:max_p_delta=max(max_p_delta,float(np.max(np.abs(p[qi,a]-p[representative[k],a]))))
            else:representative[k]=qi
    counts=np.array(list(groups.values()));sizes=counts.sum(1);conflict=(counts>0).sum(1)>1;result={'valid_candidate_rows':int(sizes.sum()),'unique_within_session_encoded_feature_candidate_groups':len(groups),'duplicate_groups':int((sizes>1).sum()),'rows_in_duplicate_groups':int(sizes[sizes>1].sum()),'conflicting_label_groups':int(conflict.sum()),'rows_in_conflicting_groups':int(sizes[conflict].sum()),'fraction_rows_in_conflicting_groups':float(sizes[conflict].sum()/sizes.sum()),'max_prediction_difference_for_equal_inputs':max_p_delta,'interpretation':'Equal complete deterministic model inputs sometimes have different realized segment labels. This demonstrates information loss/irreducible ambiguity on this sample, not a population Bayes accuracy bound. Counts depend on periodic query weights; no new feature or model is trained.'}
    write(OUT/'metrics/feature_collisions.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
