"""Deterministic safety and scientific-comparison contracts for Phase2.9A."""

import hashlib
import random

FORBIDDEN=("operation","dominant_operation","next_operation","repeat","automation","scenario",
           "window_title","keyboard","mouse","path","filename","basename","document","file_id",
           "inode","dev_major","dev_minor","session_id")
ORACLE_ALLOW={"expert_training_target","oracle_routing_evaluation","workload_classifier_target","final_scoring"}


def feature_names_allowed(names):
    return not any(token in name.lower() for name in names for token in FORBIDDEN)


def no_future_slice(rows,index): return rows[:index]
def oracle_allowed(location): return location in ORACLE_ALLOW
def predicted_route_uses_oracle(route): return any("oracle" in str(k).lower() for k in route)
def candidate_hashes_equal(hashes): return len(set(hashes.values()))<=1
def reclaim_counts_equal(counts): return len(set(counts))<=1
def oracle_rank(reuse): return sorted(range(len(reuse)),key=lambda i:(reuse[i] is None,reuse[i] if reuse[i] is not None else float("inf"),i))
def hashes_differ(left,right): return left!=right
def independent_models(left,right): return left!=right
def matched_expert(workload): return "EXPERT_"+workload


def cross_matrix_complete(classes,matrix):
    return set(matrix)==set(classes) and all(set(matrix[row])==set(classes) for row in classes)


def wrong_expert(workload,classes):
    other=next(x for x in sorted(classes) if x!=workload); return matched_expert(other)


def soft_weights(probabilities):
    total=sum(max(0,float(v)) for v in probabilities.values())
    if total<=0: return {}
    names=sorted(probabilities); output={}; consumed=0.0
    for name in names[:-1]: output[name]=max(0,float(probabilities[name]))/total; consumed+=output[name]
    output[names[-1]]=1.0-consumed; return output


def route_with_fallback(probabilities,high,low):
    confidence=max(probabilities.values()) if probabilities else 0
    if confidence>=high: return "HARD_EXPERT"
    if confidence>=low: return "TOP2_SOFT"
    return "GLOBAL_EXPERT"


def ttl_route(age,ttl): return "BASE_NATIVE_RECENCY" if age>ttl else "ACTIVE_ROUTE"
def candidate_allowed(state,version_ok,partition_ok): return state!="NOT_OBSERVED" and version_ok and partition_ok
def version_valid(predicted,current): return predicted==current
def partition_valid(predicted,current): return predicted==current


def next_reuse(states,window_seconds):
    for index,state in enumerate(states,1):
        if state is True: return index*window_seconds
    return None


def censored_target(states,window_seconds,horizon):
    steps=horizon//window_seconds; value=next_reuse(states[:steps],window_seconds)
    return value if value is not None else "CENSORED_NO_REUSE_WITHIN_HORIZON"


def pairwise_target(left,right):
    if left==right: return 0
    if left is None: return -1
    if right is None: return 1
    return 1 if left<right else -1


def refault_comparable(reclaimed_by_policy): return reclaim_counts_equal(list(reclaimed_by_policy.values()))


def block_bootstrap(blocks,rounds,seed):
    names=sorted(blocks); rng=random.Random(seed); output=[]
    for _ in range(rounds):
        sampled=[rng.choice(names) for _ in names]; values=[v for name in sampled for v in blocks[name]]
        output.append(sum(values)/len(values) if values else 0)
    return output


def selection_sessions(): return ("wps_01","wps_02","files_01")
def phase28_unchanged(before,after): return before==after


def report_strictly_better(candidate,baseline,lower_is_better=False):
    return candidate<baseline if lower_is_better else candidate>baseline


def stable_hash(payload): return hashlib.sha256(str(payload).encode()).hexdigest()

