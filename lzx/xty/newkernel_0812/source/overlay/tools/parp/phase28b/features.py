"""Causal, identity-free Phase2.8B window feature extraction."""

from collections import defaultdict
import math


def slog(value):
    return math.copysign(math.log1p(abs(value)), value) if value else 0.0


def jaccard(left, right):
    union=left|right
    return len(left&right)/len(union) if union else 1.0


def entropy(values):
    total=sum(values)
    if total<=0: return 0.0
    return -sum((x/total)*math.log(x/total) for x in values if x>0)/max(1.0,math.log(max(2,len(values))))


def safe_number(value):
    return float(value) if isinstance(value,(int,float)) and math.isfinite(value) else 0.0


class FeatureExtractor:
    """Keeps FileId only in private history; emitted vectors contain no identity."""
    def __init__(self, topk):
        self.topk=topk; self.previous=None; self.file_history={}; self.base_history=[]

    def _slot(self, output, prefix, row, rank, start):
        key=row["key"]; hist=self.file_history.get(key,{})
        output[prefix+"valid"]=1.0
        output[prefix+"size_log"]=math.log1p(row["file_size_bytes"])
        output[prefix+"pages_log"]=math.log1p(row["file_page_count"])
        output[prefix+"active_pages_log"]=math.log1p(row["unique_accessed_pages"])
        output[prefix+"observed_pages_log"]=math.log1p(row["observed_pages"])
        output[prefix+"coverage"]=row["coverage"]
        output[prefix+"weighted_coverage"]=row["weighted_coverage"]
        output[prefix+"regions_log"]=math.log1p(row["region_count"])
        output[prefix+"mean_access_ratio"]=row["mean_access_ratio"]
        output[prefix+"max_access_ratio"]=row["max_access_ratio"]
        output[prefix+"mean_age_log"]=math.log1p(row["mean_age"])
        output[prefix+"max_age_log"]=math.log1p(row["max_age"])
        output[prefix+"active_l10"]=row["active_segments"]["10"]/10.0
        output[prefix+"active_l100"]=row["active_segments"]["100"]/100.0
        output[prefix+"activity_share"]=row["activity_share"]
        for index,value in enumerate(row["coverage_l10"]): output[prefix+"shape_%02d"%index]=value
        for index,value in enumerate(row["weighted_coverage_l10"]): output[prefix+"weighted_shape_%02d"%index]=value
        previous_rank=hist.get("rank",self.topk+1)
        output[prefix+"previous_rank_norm"]=previous_rank/max(1,self.topk+1)
        output[prefix+"rank_changed"]=float(previous_rank!=rank)
        output[prefix+"inactive_time_log"]=math.log1p(max(0,start-hist.get("last_active",start))/1e9)
        output[prefix+"active_streak_log"]=math.log1p(hist.get("streak",0)+int(row["unique_accessed_pages"]>0))
        nowbits=int(row["active_bits"]["100"],16); oldbits=hist.get("bits",0)
        output[prefix+"segment_jaccard_previous"]=jaccard({i for i in range(100) if nowbits>>i&1},{i for i in range(100) if oldbits>>i&1})
        output[prefix+"activity_delta_signed_log"]=slog(row["weighted_pages"]-hist.get("weighted",0))

    def extract(self, row, version):
        files=row["files"]; top=files[:self.topk]; rest=files[self.topk:]; start=row["window_start_ns"]
        values={"foreground_app_id":float(row["app_id"]),
                "file_object_count_log":math.log1p(len(files)),
                "file_region_count_log":math.log1p(row["file_region_count"]),
                "anon_region_count_log":math.log1p(row["anon_region_count"]),
                "anon_pages_log":math.log1p(row["anon"]["pages"]),
                "anon_weighted_pages_log":math.log1p(row["anon"]["weighted_pages"]),
                "anon_active_ratio":row["anon"]["active_ratio"],
                "anon_mean_age_log":math.log1p(row["anon"]["mean_age"]),
                "anon_object_count_log":math.log1p(row["anon"]["object_count"]),
                "damon_sample_count_log":math.log1p(row["damon_sample_count"]),
                "kernel_sample_count_log":math.log1p(row["kernel_sample_count"])}
        for rank in range(1,self.topk+1):
            prefix="slot%d_"%rank
            if rank<=len(top): self._slot(values,prefix,top[rank-1],rank,start)
            else:
                for name in ("valid","size_log","pages_log","active_pages_log","observed_pages_log","coverage","weighted_coverage","regions_log","mean_access_ratio","max_access_ratio","mean_age_log","max_age_log","active_l10","active_l100","activity_share","previous_rank_norm","rank_changed","inactive_time_log","active_streak_log","segment_jaccard_previous","activity_delta_signed_log"):
                    values[prefix+name]=0.0
                for index in range(10): values[prefix+"shape_%02d"%index]=0.0; values[prefix+"weighted_shape_%02d"%index]=0.0
        shares=[x["weighted_pages"] for x in rest]
        values.update({"other_count_log":math.log1p(len(rest)),
            "other_active_count_log":math.log1p(sum(x["unique_accessed_pages"]>0 for x in rest)),
            "other_active_pages_log":math.log1p(sum(x["unique_accessed_pages"] for x in rest)),
            "other_weighted_pages_log":math.log1p(sum(x["weighted_pages"] for x in rest)),
            "other_regions_log":math.log1p(sum(x["region_count"] for x in rest)),
            "other_active_l10_log":math.log1p(sum(x["active_segments"]["10"] for x in rest)),
            "other_active_l100_log":math.log1p(sum(x["active_segments"]["100"] for x in rest)),
            "other_mean_coverage":sum(x["coverage"] for x in rest)/max(1,len(rest)),
            "other_max_coverage":max([x["coverage"] for x in rest] or [0]),
            "other_head_activity":sum(sum(x["weighted_coverage_l10"][:3]) for x in rest),
            "other_middle_activity":sum(sum(x["weighted_coverage_l10"][3:7]) for x in rest),
            "other_tail_activity":sum(sum(x["weighted_coverage_l10"][7:]) for x in rest),
            "other_activity_entropy":entropy(shares),
            "top1_activity_share":sum(x["activity_share"] for x in files[:1]),
            "top3_activity_share":sum(x["activity_share"] for x in files[:3]),
            "topk_activity_share":sum(x["activity_share"] for x in top)})
        current_keys={x["key"] for x in top}; previous_keys=set(self.previous["keys"]) if self.previous else set()
        current_segments={(x["key"],i) for x in top for i in range(100) if int(x["active_bits"]["100"],16)>>i&1}
        previous_segments=set(self.previous["segments"]) if self.previous else set()
        centroid_num=sum(sum((i+.5)/10*v for i,v in enumerate(x["weighted_coverage_l10"])) for x in top)
        centroid_den=sum(sum(x["weighted_coverage_l10"]) for x in top); centroid=centroid_num/centroid_den if centroid_den else 0
        old_centroid=self.previous["centroid"] if self.previous else centroid
        values.update({"file_set_jaccard_previous":jaccard(current_keys,previous_keys),
            "segment_set_jaccard_previous":jaccard(current_segments,previous_segments),
            "topk_rank_churn":sum((self.file_history.get(x["key"],{}).get("rank",i)!=i) for i,x in enumerate(top,1))/max(1,len(top)),
            "same_slot_retention":sum(i<=len(top) and i<=len(self.previous["ordered"]) and top[i-1]["key"]==self.previous["ordered"][i-1] for i in range(1,self.topk+1))/self.topk if self.previous else 0,
            "access_centroid":centroid,"access_centroid_shift":centroid-old_centroid,
            "active_range_width":(max([p for _,p in current_segments])-min([p for _,p in current_segments])+1)/100 if current_segments else 0,
            "access_entropy":entropy([x["activity_share"] for x in top])})
        if version in ("V2_PAGE_VM","V3_FULL_CURRENT","V4_FULL_TEMPORAL"):
            for name,value in row["kernel"]["values"].items():
                if version=="V2_PAGE_VM" and not any(x in name for x in ("memory_","pgfault","pgmajfault","refault","pgscan","pgsteal")): continue
                values["kernel_"+name]=slog(value) if name.endswith(("_delta","_rate")) else math.log1p(max(0,value))
            for name,available in row["kernel"]["availability"].items():
                if version=="V2_PAGE_VM" and not any(x in name for x in ("memory_","pgfault","pgmajfault","refault","pgscan","pgsteal")): continue
                values["available_"+name]=float(available)
        base=dict(values)
        if version=="V4_FULL_TEMPORAL":
            history=self.base_history[-6:]
            temporal_names=("anon_active_ratio","topk_activity_share","access_centroid","access_entropy","file_set_jaccard_previous","segment_set_jaccard_previous")
            for lag in (1,2,3,6):
                prior=history[-lag] if len(history)>=lag else {}
                for name in temporal_names: values["history%d_"%lag+name]=prior.get(name,0.0)
            for length in (3,6):
                subset=history[-length:]
                for name in temporal_names: values["mean%d_"%length+name]=sum(x.get(name,0) for x in subset)/max(1,len(subset))
        for rank,item in enumerate(top,1):
            old=self.file_history.get(item["key"],{}); active=item["unique_accessed_pages"]>0
            self.file_history[item["key"]]={"rank":rank,"bits":int(item["active_bits"]["100"],16),"weighted":item["weighted_pages"],
                "last_active":start if active else old.get("last_active",start),"streak":old.get("streak",0)+1 if active else 0}
        self.previous={"keys":list(current_keys),"segments":list(current_segments),"centroid":centroid,"ordered":[x["key"] for x in top]}
        self.base_history.append(base)
        return values


def source_type(name):
    if name=="foreground_app_id": return "UPPER_LAYER_APP_ID"
    if name.startswith("anon_"): return "PARP_ANON"
    if name.startswith("kernel_") or name.startswith("available_"): return "CGROUP_KERNEL_METRIC"
    if name.startswith("history") or name.startswith("mean") or "previous" in name or "churn" in name or "retention" in name: return "DERIVED_KERNEL_HISTORY"
    return "PARP_FILE"

