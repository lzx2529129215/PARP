"""Operation-independent access-pattern weak taxonomy."""


def classify_kernel_pattern(features):
    shift = features.get("centroid_shift", 0.0)
    continuity = features.get("continuity", 0.0)
    delta = features.get("working_set_delta", 0.0)
    write_burst = features.get("write_burst", 0.0)
    entropy = features.get("access_entropy", 0.0)
    if write_burst > .5: return "BURST_WRITE"
    if delta > .5: return "EXPANDING_WORKING_SET"
    if delta < -.5: return "CONTRACTING_WORKING_SET"
    if continuity > .7 and shift > .5: return "SEQUENTIAL_FORWARD"
    if continuity > .7 and shift < -.5: return "SEQUENTIAL_BACKWARD"
    if entropy > .8: return "RANDOM_JUMP"
    if continuity > .7: return "LOCAL_LOOP"
    if features.get("active_ratio", 0.0) == 0: return "IDLE_COOLING"
    return "MIXED"
