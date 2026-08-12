"""Strict online and feature leakage contracts."""

FORBIDDEN = ("operation", "action", "automation", "scenario", "label",
             "ground_truth", "next_op", "current_op", "dominant_op",
             "window_title", "keyboard", "mouse", "document_name",
             "filename", "path", "file_id", "inode", "dev_major",
             "dev_minor")
SAFE_SOURCES = {"UPPER_LAYER_APP_ID", "PARP_FILE", "PARP_ANON",
                "CGROUP_MEMORY", "CGROUP_CPU", "CGROUP_IO", "CGROUP_PSI",
                "PROC_KERNEL", "DERIVED_KERNEL_HISTORY", "AVAILABILITY_MASK"}


def validate_feature_names(names):
    for name in names:
        lowered = name.lower()
        match = next((word for word in FORBIDDEN if word in lowered), None)
        if match:
            raise ValueError("forbidden model feature %s (%s)" % (name, match))
    if len(set(names)) != len(list(names)):
        raise ValueError("duplicate model feature")
    return True


def validate_online_input(payload):
    required = {"foreground_app_id", "kernel_features", "past_predictions"}
    if set(payload) != required:
        raise ValueError("only foreground_app_id, kernel features and past predictions are allowed")
    if not isinstance(payload["foreground_app_id"], int):
        raise ValueError("invalid foreground_app_id")
    validate_feature_names(payload["kernel_features"].keys())
    return True


def validate_source_map(names, source_map):
    validate_feature_names(names)
    if set(names) != set(source_map):
        raise ValueError("incomplete feature source map")
    upper = []
    for name, source in source_map.items():
        if source.get("source_type") not in SAFE_SOURCES:
            raise ValueError("unsafe feature source")
        if source["source_type"] == "UPPER_LAYER_APP_ID": upper.append(name)
    if upper != ["foreground_app_id"]:
        raise ValueError("foreground_app_id is the sole upper-layer semantic feature")
    return True
