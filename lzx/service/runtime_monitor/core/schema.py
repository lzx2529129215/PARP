"""CSV schemas for Runtime Monitor v0 — model/ and review/ output files."""

# ---------------------------------------------------------------------------
# model/ — machine-training data
# ---------------------------------------------------------------------------

GLOBAL_STATE_1S_FIELDS = [
    "session_id",
    "feature_window_id",
    "window_start_ns",
    "window_end_ns",
    "timestamp",
    "foreground_app",
    "foreground_duration_ms",
    "foreground_window_id",
    "foreground_pid",
    "foreground_wm_class",
    "foreground_window_title",
    "observed_apps",
    "open_apps",
    "closed_apps",
    "newly_opened_apps",
    "newly_closed_apps",
    "app_history",
    "duration_history_ms",
    "current_operation_label",
    "current_operation_app",
    "current_action",
    "state_label",
    "manual_label",
    "scenario_id",
    "step_id",
    "global_mem_available_kb",
    "global_pgmajfault_delta",
    "global_pswpin_delta",
    "global_pswpout_delta",
    "global_pgscan_delta",
    "global_pgsteal_delta",
    "test_slice",
    "test_slice_path",
    "test_mem_current",
    "test_mem_high",
    "test_mem_max",
]

APP_STATE_1S_FIELDS = [
    "session_id",
    "feature_window_id",
    "window_start_ns",
    "window_end_ns",
    "timestamp",
    "app_id",
    "app_display_name",
    "is_open",
    "is_foreground",
    "is_label_target_app",
    "closed",
    "pid_count",
    "pids",
    "tgids",
    "comm",
    "exe_path",
    "cmdline_hash",
    "app_cgroup_unit",
    "app_cgroup_path",
    "test_slice",
    "in_test_slice",
    "open_cnt_1s",
    "read_bytes_1s",
    "write_bytes_1s",
    "rchar_1s",
    "wchar_1s",
    "mmap_cnt_1s",
    "fsync_cnt_1s",
    "rename_cnt_1s",
    "unique_inode_cnt_1s",
    "docx_open_cnt_1s",
    "tmp_open_cnt_1s",
    "so_open_cnt_1s",
    "font_open_cnt_1s",
    "pdf_open_cnt_1s",
    "mem_current",
    "anon",
    "file",
    "active_file",
    "inactive_file",
    "pgmajfault_delta",
    "refault_file_delta",
    "current_operation_label",
    "current_operation_app",
    "state_label",
    "manual_label",
    "label_app",
]

FOREGROUND_EVENT_FIELDS = [
    "session_id",
    "ts_ns",
    "timestamp",
    "event_type",
    "old_app",
    "new_app",
    "foreground_app",
    "duration_ms",
    "window_id",
    "window_title",
    "wm_class",
    "pid",
    "tgid",
    "source",
]

DIRECT_APP_EVENT_FIELDS = [
    "session_id",
    "event_id",
    "ts_ns",
    "timestamp",
    "event_type",
    "app",
    "old_app",
    "new_app",
    "foreground_app",
    "duration_ms",
    "window_id",
    "window_title",
    "pid",
    "open_apps_before",
    "open_apps_after",
    "native_event",
    "source",
    "prediction_triggered",
    "prediction_id",
    "prediction_status",
]

PROCESS_EVENT_FIELDS = [
    "session_id",
    "ts_ns",
    "timestamp",
    "event_type",
    "app",
    "pid",
    "tgid",
    "comm",
    "cmdline_hash",
    "exe_path",
    "cgroup_unit",
    "cgroup_path",
    "test_slice",
    "in_test_slice",
    "source",
]

APP_LIFECYCLE_EVENT_FIELDS = [
    "session_id",
    "ts_ns",
    "timestamp",
    "event_type",
    "app",
    "open_apps_before",
    "open_apps_after",
    "pid_count_before",
    "pid_count_after",
    "source",
]

OPERATION_EVENT_FIELDS = [
    "session_id",
    "operation_id",
    "scenario_id",
    "step_id",
    "operation_label",
    "operation_app",
    "action",
    "start_ns",
    "end_ns",
    "start_time",
    "end_time",
    "duration_ms",
    "status",
    "optional",
    "foreground_app_at_start",
    "foreground_app_at_end",
    "open_apps_at_start",
    "open_apps_at_end",
    "source",
]

OPERATION_LABEL_FIELDS = [
    "session_id",
    "operation_id",
    "scenario_id",
    "step_id",
    "operation_label",
    "operation_app",
    "action",
    "start_ns",
    "end_ns",
    "start_time",
    "end_time",
    "duration_ms",
    "status",
    "optional",
    "source",
]

FOREGROUND_DEBUG_FIELDS = [
    "session_id",
    "feature_window_id",
    "ts_ns",
    "timestamp",
    "active_window_id_xdotool",
    "active_window_id_xprop_root",
    "chosen_window_id",
    "xdotool_window_name",
    "xprop_net_wm_name",
    "xprop_wm_name",
    "wm_class",
    "net_wm_pid",
    "xdotool_pid",
    "pid_comm",
    "pid_cmdline",
    "mapped_app",
    "previous_foreground_app",
    "foreground_app",
    "window_title",
    "error",
]

# ---------------------------------------------------------------------------
# Legacy aliases (kept for backward compatibility with existing scripts)
# ---------------------------------------------------------------------------

# Old field lists — retained so imports in scripts/ and tests/ don't break
FEATURE_FIELDS = GLOBAL_STATE_1S_FIELDS
APP_FEATURE_FIELDS = APP_STATE_1S_FIELDS
APP_EVENT_FIELDS = FOREGROUND_EVENT_FIELDS
EVENT_FIELDS = [
    "ts_ns",
    "pid",
    "tgid",
    "app",
    "comm",
    "event",
    "path",
    "ext",
    "inode",
    "offset",
    "size",
]

# ---------------------------------------------------------------------------
# review/ — human-inspection data
# ---------------------------------------------------------------------------

TIMELINE_FIELDS = [
    "time",
    "foreground_app",
    "window_title",
    "opened_apps",
    "current_operation",
    "operation_app",
    "note",
]

APP_SWITCHES_FIELDS = [
    "time",
    "from_app",
    "to_app",
    "window_title",
    "duration_s",
    "expected_operation",
    "result",
    "note",
]

OPENED_APPS_TIMELINE_FIELDS = [
    "time",
    "event",
    "app",
    "opened_apps_before",
    "opened_apps_after",
    "result",
    "note",
]

OPERATIONS_TIMELINE_FIELDS = [
    "start_time",
    "end_time",
    "app",
    "operation",
    "action",
    "status",
    "foreground_during_operation",
    "opened_apps",
    "result",
    "note",
]

CHECKS_FIELDS = [
    "check_name",
    "expected",
    "observed",
    "result",
    "details",
]

FOREGROUND_DEBUG_BRIEF_FIELDS = [
    "time",
    "expected_app",
    "foreground_app",
    "window_title",
    "wm_class",
    "pid",
    "result",
    "note",
]
