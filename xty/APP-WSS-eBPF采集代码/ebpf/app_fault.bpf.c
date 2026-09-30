#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#include "app_fault.h"

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct app_fault_config);
} config_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct app_fault_counters);
} counters_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_CGROUP_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} target_cgroup_map SEC(".maps");

SEC("tracepoint/exceptions/page_fault_user")
int count_target_page_fault(void *ctx)
{
    const __u32 key = 0;
    struct app_fault_config *config;
    struct app_fault_counters *counters;

    (void)ctx;
    config = bpf_map_lookup_elem(&config_map, &key);
    if (!config || !config->enabled)
        return 0;
    if (config->match_mode == APP_CGROUP_MATCH_SUBTREE) {
        if (!bpf_current_task_under_cgroup(&target_cgroup_map, 0))
            return 0;
    } else if (bpf_get_current_cgroup_id() != config->target_cgroup_id) {
        return 0;
    }

    counters = bpf_map_lookup_elem(&counters_map, &key);
    if (counters)
        counters->fault_total++;
    return 0;
}

SEC("kprobe/__vmf_anon_prepare")
int count_target_anon_handler(void *ctx)
{
    const __u32 key = 0;
    struct app_fault_config *config;
    struct app_fault_counters *counters;

    (void)ctx;
    config = bpf_map_lookup_elem(&config_map, &key);
    if (!config || !config->enabled)
        return 0;
    if (config->match_mode == APP_CGROUP_MATCH_SUBTREE) {
        if (!bpf_current_task_under_cgroup(&target_cgroup_map, 0))
            return 0;
    } else if (bpf_get_current_cgroup_id() != config->target_cgroup_id) {
        return 0;
    }

    counters = bpf_map_lookup_elem(&counters_map, &key);
    if (counters)
        counters->fault_anon_handler++;
    return 0;
}

SEC("kprobe/filemap_fault")
int count_target_file_handler(void *ctx)
{
    const __u32 key = 0;
    struct app_fault_config *config;
    struct app_fault_counters *counters;

    (void)ctx;
    config = bpf_map_lookup_elem(&config_map, &key);
    if (!config || !config->enabled)
        return 0;
    if (config->match_mode == APP_CGROUP_MATCH_SUBTREE) {
        if (!bpf_current_task_under_cgroup(&target_cgroup_map, 0))
            return 0;
    } else if (bpf_get_current_cgroup_id() != config->target_cgroup_id) {
        return 0;
    }

    counters = bpf_map_lookup_elem(&counters_map, &key);
    if (counters)
        counters->fault_file_handler++;
    return 0;
}

SEC("kprobe/do_wp_page")
int count_target_wp_handler(void *ctx)
{
    const __u32 key = 0;
    struct app_fault_config *config;
    struct app_fault_counters *counters;

    (void)ctx;
    config = bpf_map_lookup_elem(&config_map, &key);
    if (!config || !config->enabled)
        return 0;
    if (config->match_mode == APP_CGROUP_MATCH_SUBTREE) {
        if (!bpf_current_task_under_cgroup(&target_cgroup_map, 0))
            return 0;
    } else if (bpf_get_current_cgroup_id() != config->target_cgroup_id) {
        return 0;
    }

    counters = bpf_map_lookup_elem(&counters_map, &key);
    if (counters)
        counters->fault_wp_handler++;
    return 0;
}

SEC("kprobe/shmem_fault")
int count_target_shmem_handler(void *ctx)
{
    const __u32 key = 0;
    struct app_fault_config *config;
    struct app_fault_counters *counters;

    (void)ctx;
    config = bpf_map_lookup_elem(&config_map, &key);
    if (!config || !config->enabled)
        return 0;
    if (config->match_mode == APP_CGROUP_MATCH_SUBTREE) {
        if (!bpf_current_task_under_cgroup(&target_cgroup_map, 0))
            return 0;
    } else if (bpf_get_current_cgroup_id() != config->target_cgroup_id) {
        return 0;
    }

    counters = bpf_map_lookup_elem(&counters_map, &key);
    if (counters)
        counters->fault_shmem_handler++;
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
