#ifndef APP_FAULT_H
#define APP_FAULT_H

#include <linux/types.h>

struct app_fault_config {
    __u64 target_cgroup_id;
    __u64 enabled;
    __u64 match_mode;
};

#define APP_CGROUP_MATCH_EXACT 0
#define APP_CGROUP_MATCH_SUBTREE 1

struct app_fault_counters {
    __u64 fault_total;
    __u64 fault_anon_handler;
    __u64 fault_file_handler;
    __u64 fault_wp_handler;
    __u64 fault_shmem_handler;
};

#endif
