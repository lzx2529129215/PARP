#define _GNU_SOURCE

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "app_fault.h"

#define HOOK_TYPE "tracepoint"
#define HOOK_NAME "exceptions/page_fault_user"

static volatile sig_atomic_t stop_requested;

struct options {
    const char *bpf_object;
    const char *cgroup_path;
    const char *app_id;
    const char *app_name;
    const char *run_id;
    const char *operation_id;
    const char *output;
    int interval_ms;
    int duration_seconds;
    int match_mode;
};

static void on_signal(int signo)
{
    (void)signo;
    stop_requested = 1;
}

static uint64_t clock_ns(clockid_t clock_id)
{
    struct timespec ts;

    if (clock_gettime(clock_id, &ts) != 0)
        return 0;
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int sleep_until(uint64_t deadline_ns)
{
    struct timespec target = {
        .tv_sec = (time_t)(deadline_ns / 1000000000ULL),
        .tv_nsec = (long)(deadline_ns % 1000000000ULL),
    };

    while (!stop_requested) {
        int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &target, NULL);
        if (rc == 0)
            return 0;
        if (rc != EINTR) {
            errno = rc;
            return -1;
        }
    }
    return 0;
}

static uint64_t cgroup_id_from_path(const char *path)
{
    union {
        uint64_t value;
        unsigned char bytes[8];
    } id = {0};
    struct file_handle *handle = NULL;
    int mount_id = 0;
    int rc;

    handle = calloc(1, sizeof(*handle));
    if (!handle)
        return 0;
    rc = name_to_handle_at(AT_FDCWD, path, handle, &mount_id, 0);
    if (rc == 0 || errno != EOVERFLOW || handle->handle_bytes != sizeof(id.bytes)) {
        free(handle);
        return 0;
    }
    handle = realloc(handle, sizeof(*handle) + handle->handle_bytes);
    if (!handle)
        return 0;
    rc = name_to_handle_at(AT_FDCWD, path, handle, &mount_id, 0);
    if (rc != 0) {
        free(handle);
        return 0;
    }
    memcpy(id.bytes, handle->f_handle, sizeof(id.bytes));
    free(handle);
    return id.value;
}

static int read_text_file(const char *path, char *buffer, size_t size)
{
    FILE *stream = fopen(path, "r");
    size_t length;

    if (!stream)
        return -1;
    length = fread(buffer, 1, size - 1, stream);
    if (ferror(stream)) {
        fclose(stream);
        return -1;
    }
    buffer[length] = '\0';
    fclose(stream);
    return 0;
}

static long long read_integer_file(const char *path)
{
    char buffer[128];
    char *end = NULL;
    long long value;

    if (read_text_file(path, buffer, sizeof(buffer)) != 0)
        return -1;
    errno = 0;
    value = strtoll(buffer, &end, 10);
    return errno == 0 && end != buffer ? value : -1;
}

struct pid_list {
    pid_t *values;
    size_t count;
    size_t capacity;
};

static int pid_list_add(struct pid_list *list, pid_t pid)
{
    size_t index;

    for (index = 0; index < list->count; index++) {
        if (list->values[index] == pid)
            return 0;
    }
    if (list->count == list->capacity) {
        size_t capacity = list->capacity ? list->capacity * 2 : 32;
        pid_t *values = realloc(list->values, capacity * sizeof(*values));
        if (!values)
            return -1;
        list->values = values;
        list->capacity = capacity;
    }
    list->values[list->count++] = pid;
    return 0;
}

static int collect_subtree_pids(const char *path, struct pid_list *pids)
{
    char procs_path[PATH_MAX];
    DIR *directory;
    struct dirent *entry;
    FILE *stream;
    int status = 0;

    if (snprintf(procs_path, sizeof(procs_path), "%s/cgroup.procs", path) >=
                (int)sizeof(procs_path))
        return -1;
    stream = fopen(procs_path, "r");
    if (stream) {
        long value;
        while (fscanf(stream, "%ld", &value) == 1) {
            if (value > 0 && value <= INT_MAX &&
                pid_list_add(pids, (pid_t)value) != 0)
                status = -1;
        }
        fclose(stream);
    }
    directory = opendir(path);
    if (!directory)
        return status;
    while ((entry = readdir(directory)) != NULL) {
        char child_path[PATH_MAX];

        if (entry->d_name[0] == '.' ||
            (entry->d_type != DT_DIR && entry->d_type != DT_UNKNOWN))
            continue;
        if (snprintf(child_path, sizeof(child_path), "%s/%s", path,
                     entry->d_name) >= (int)sizeof(child_path)) {
            status = -1;
            continue;
        }
        if (collect_subtree_pids(child_path, pids) != 0)
            status = -1;
    }
    closedir(directory);
    return status;
}

static int read_proc_rss_pages(pid_t pid, unsigned long long *resident_pages)
{
    char path[64];
    unsigned long long size_pages;
    FILE *stream;

    if (snprintf(path, sizeof(path), "/proc/%ld/statm", (long)pid) >=
                (int)sizeof(path))
        return -1;
    stream = fopen(path, "r");
    if (!stream)
        return -1;
    if (fscanf(stream, "%llu %llu", &size_pages, resident_pages) != 2) {
        fclose(stream);
        return -1;
    }
    fclose(stream);
    return 0;
}

static long long memory_stat_value(const char *cgroup_path, const char *wanted)
{
    char path[PATH_MAX];
    char name[128];
    long long value;
    FILE *stream;

    if (snprintf(path, sizeof(path), "%s/memory.stat", cgroup_path) >= (int)sizeof(path))
        return -1;
    stream = fopen(path, "r");
    if (!stream)
        return -1;
    while (fscanf(stream, "%127s %lld", name, &value) == 2) {
        if (strcmp(name, wanted) == 0) {
            fclose(stream);
            return value;
        }
    }
    fclose(stream);
    return -1;
}

struct psi_values {
    double avg10;
    long long total_us;
};

static struct psi_values memory_psi_values(const char *cgroup_path,
                                           const char *wanted)
{
    char path[PATH_MAX];
    char line[512];
    FILE *stream;
    struct psi_values result = {.avg10 = -1.0, .total_us = -1};

    if (snprintf(path, sizeof(path), "%s/memory.pressure", cgroup_path) >= (int)sizeof(path))
        return result;
    stream = fopen(path, "r");
    if (!stream)
        return result;
    while (fgets(line, sizeof(line), stream)) {
        char kind[16];
        char *avg10;
        char *total;

        if (sscanf(line, "%15s", kind) == 1 && strcmp(kind, wanted) == 0) {
            avg10 = strstr(line, "avg10=");
            total = strstr(line, "total=");
            if (avg10)
                result.avg10 = strtod(avg10 + strlen("avg10="), NULL);
            if (total)
                result.total_us = strtoll(total + strlen("total="), NULL, 10);
            fclose(stream);
            return result;
        }
    }
    fclose(stream);
    return result;
}

static int read_counter_totals(int map_fd, int cpu_count,
                               struct app_fault_counters *per_cpu,
                               struct app_fault_counters *totals)
{
    const uint32_t key = 0;
    int cpu;

    memset(per_cpu, 0, sizeof(*per_cpu) * (size_t)cpu_count);
    if (bpf_map_lookup_elem(map_fd, &key, per_cpu) != 0)
        return -1;
    memset(totals, 0, sizeof(*totals));
    for (cpu = 0; cpu < cpu_count; cpu++) {
        totals->fault_total += per_cpu[cpu].fault_total;
        totals->fault_anon_handler += per_cpu[cpu].fault_anon_handler;
        totals->fault_file_handler += per_cpu[cpu].fault_file_handler;
        totals->fault_wp_handler += per_cpu[cpu].fault_wp_handler;
        totals->fault_shmem_handler += per_cpu[cpu].fault_shmem_handler;
    }
    return 0;
}

static void csv_string(FILE *stream, const char *value)
{
    const char *cursor = value ? value : "";

    fputc('"', stream);
    while (*cursor) {
        if (*cursor == '"')
            fputc('"', stream);
        fputc(*cursor++, stream);
    }
    fputc('"', stream);
}

static void usage(const char *program)
{
    fprintf(stderr,
            "Usage: %s --cgroup PATH --app-id ID --app-name NAME --run-id ID "
            "--operation-id ID --output CSV [--interval-ms N] [--duration-seconds N] "
            "[--cgroup-match exact|subtree] [--bpf-object PATH]\n", program);
}

static int parse_match_mode(const char *value)
{
    if (strcmp(value, "exact") == 0)
        return APP_CGROUP_MATCH_EXACT;
    if (strcmp(value, "subtree") == 0)
        return APP_CGROUP_MATCH_SUBTREE;
    return -1;
}

static int parse_options(int argc, char **argv, struct options *options)
{
    enum {
        OPT_CGROUP = 1000, OPT_APP_ID, OPT_APP_NAME, OPT_RUN_ID,
        OPT_OPERATION_ID, OPT_OUTPUT, OPT_INTERVAL, OPT_DURATION, OPT_BPF_OBJECT,
        OPT_CGROUP_MATCH,
    };
    static const struct option long_options[] = {
        {"cgroup", required_argument, NULL, OPT_CGROUP},
        {"app-id", required_argument, NULL, OPT_APP_ID},
        {"app-name", required_argument, NULL, OPT_APP_NAME},
        {"run-id", required_argument, NULL, OPT_RUN_ID},
        {"operation-id", required_argument, NULL, OPT_OPERATION_ID},
        {"output", required_argument, NULL, OPT_OUTPUT},
        {"interval-ms", required_argument, NULL, OPT_INTERVAL},
        {"duration-seconds", required_argument, NULL, OPT_DURATION},
        {"cgroup-match", required_argument, NULL, OPT_CGROUP_MATCH},
        {"bpf-object", required_argument, NULL, OPT_BPF_OBJECT},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0},
    };
    int option;

    *options = (struct options) {
        .bpf_object = "build/app_fault.bpf.o",
        .interval_ms = 1000,
        .duration_seconds = 0,
        .match_mode = APP_CGROUP_MATCH_SUBTREE,
    };
    while ((option = getopt_long(argc, argv, "h", long_options, NULL)) != -1) {
        switch (option) {
        case OPT_CGROUP: options->cgroup_path = optarg; break;
        case OPT_APP_ID: options->app_id = optarg; break;
        case OPT_APP_NAME: options->app_name = optarg; break;
        case OPT_RUN_ID: options->run_id = optarg; break;
        case OPT_OPERATION_ID: options->operation_id = optarg; break;
        case OPT_OUTPUT: options->output = optarg; break;
        case OPT_INTERVAL: options->interval_ms = atoi(optarg); break;
        case OPT_DURATION: options->duration_seconds = atoi(optarg); break;
        case OPT_CGROUP_MATCH: options->match_mode = parse_match_mode(optarg); break;
        case OPT_BPF_OBJECT: options->bpf_object = optarg; break;
        case 'h': usage(argv[0]); exit(0);
        default: return -1;
        }
    }
    if (!options->cgroup_path || !options->app_id || !options->app_name ||
        !options->run_id || !options->operation_id || !options->output ||
        options->interval_ms < 50 || options->duration_seconds < 0 ||
        options->match_mode < APP_CGROUP_MATCH_EXACT ||
        options->match_mode > APP_CGROUP_MATCH_SUBTREE)
        return -1;
    return 0;
}

int main(int argc, char **argv)
{
    struct options options;
    struct bpf_object *object = NULL;
    struct bpf_program *program;
    struct bpf_program *anon_program;
    struct bpf_program *file_program;
    struct bpf_program *wp_program;
    struct bpf_program *shmem_program;
    struct bpf_link *link = NULL;
    struct bpf_link *anon_link = NULL;
    struct bpf_link *file_link = NULL;
    struct bpf_link *wp_link = NULL;
    struct bpf_link *shmem_link = NULL;
    struct bpf_map *config_map;
    struct bpf_map *counters_map;
    struct bpf_map *target_cgroup_map;
    struct app_fault_config config;
    struct app_fault_counters *per_cpu = NULL;
    struct bpf_prog_info prog_info = {};
    struct bpf_map_info map_info = {};
    uint32_t info_length;
    uint32_t key = 0;
    uint64_t cgroup_id;
    struct app_fault_counters previous_counters;
    uint64_t start_ns;
    uint64_t next_ns;
    uint64_t previous_ns;
    long long previous_psi_some_total;
    long long previous_psi_full_total;
    int cpu_count;
    int config_fd;
    int counters_fd;
    int target_cgroup_fd = -1;
    int target_cgroup_map_fd;
    int program_fd;
    int sequence = 0;
    FILE *output = NULL;
    int rc = 1;

    if (parse_options(argc, argv, &options) != 0) {
        usage(argv[0]);
        return 2;
    }
    if (access(options.cgroup_path, R_OK | X_OK) != 0) {
        perror("cgroup path");
        return 2;
    }
    cgroup_id = cgroup_id_from_path(options.cgroup_path);
    if (!cgroup_id) {
        fprintf(stderr, "cannot resolve cgroup id: %s\n", options.cgroup_path);
        return 2;
    }
    if (options.match_mode == APP_CGROUP_MATCH_SUBTREE) {
        target_cgroup_fd = open(options.cgroup_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (target_cgroup_fd < 0) {
            perror("open target cgroup");
            return 2;
        }
    }

    object = bpf_object__open_file(options.bpf_object, NULL);
    if (libbpf_get_error(object)) {
        fprintf(stderr, "cannot open BPF object: %s\n", options.bpf_object);
        object = NULL;
        goto cleanup;
    }
    if (bpf_object__load(object) != 0) {
        fprintf(stderr, "cannot load BPF object\n");
        goto cleanup;
    }
    program = bpf_object__find_program_by_name(object, "count_target_page_fault");
    anon_program = bpf_object__find_program_by_name(object, "count_target_anon_handler");
    file_program = bpf_object__find_program_by_name(object, "count_target_file_handler");
    wp_program = bpf_object__find_program_by_name(object, "count_target_wp_handler");
    shmem_program = bpf_object__find_program_by_name(object, "count_target_shmem_handler");
    config_map = bpf_object__find_map_by_name(object, "config_map");
    counters_map = bpf_object__find_map_by_name(object, "counters_map");
    target_cgroup_map = bpf_object__find_map_by_name(object, "target_cgroup_map");
    if (!program || !anon_program || !file_program || !wp_program ||
        !shmem_program || !config_map || !counters_map ||
        !target_cgroup_map) {
        fprintf(stderr, "required BPF program or map not found\n");
        goto cleanup;
    }
    config_fd = bpf_map__fd(config_map);
    counters_fd = bpf_map__fd(counters_map);
    target_cgroup_map_fd = bpf_map__fd(target_cgroup_map);
    program_fd = bpf_program__fd(program);
    config = (struct app_fault_config) {
        .target_cgroup_id = cgroup_id,
        .enabled = 1,
        .match_mode = (uint64_t)options.match_mode,
    };
    if (bpf_map_update_elem(config_fd, &key, &config, BPF_ANY) != 0) {
        perror("config map update");
        goto cleanup;
    }
    if (options.match_mode == APP_CGROUP_MATCH_SUBTREE &&
        bpf_map_update_elem(target_cgroup_map_fd, &key, &target_cgroup_fd, BPF_ANY) != 0) {
        perror("target cgroup map update");
        goto cleanup;
    }
    link = bpf_program__attach_tracepoint(program, "exceptions", "page_fault_user");
    if (libbpf_get_error(link)) {
        fprintf(stderr, "cannot attach %s\n", HOOK_NAME);
        link = NULL;
        goto cleanup;
    }
    anon_link = bpf_program__attach_kprobe(anon_program, false,
                                            "__vmf_anon_prepare");
    if (libbpf_get_error(anon_link)) {
        fprintf(stderr, "cannot attach kprobe/__vmf_anon_prepare\n");
        anon_link = NULL;
        goto cleanup;
    }
    file_link = bpf_program__attach_kprobe(file_program, false, "filemap_fault");
    if (libbpf_get_error(file_link)) {
        fprintf(stderr, "cannot attach kprobe/filemap_fault\n");
        file_link = NULL;
        goto cleanup;
    }
    wp_link = bpf_program__attach_kprobe(wp_program, false, "do_wp_page");
    if (libbpf_get_error(wp_link)) {
        fprintf(stderr, "cannot attach kprobe/do_wp_page\n");
        wp_link = NULL;
        goto cleanup;
    }
    shmem_link = bpf_program__attach_kprobe(shmem_program, false, "shmem_fault");
    if (libbpf_get_error(shmem_link)) {
        fprintf(stderr, "cannot attach kprobe/shmem_fault\n");
        shmem_link = NULL;
        goto cleanup;
    }

    cpu_count = libbpf_num_possible_cpus();
    if (cpu_count <= 0) {
        fprintf(stderr, "cannot determine possible CPU count\n");
        goto cleanup;
    }
    per_cpu = calloc((size_t)cpu_count, sizeof(*per_cpu));
    if (!per_cpu)
        goto cleanup;
    if (read_counter_totals(counters_fd, cpu_count, per_cpu, &previous_counters) != 0) {
        perror("counter map lookup");
        goto cleanup;
    }
    previous_psi_some_total = memory_psi_values(options.cgroup_path, "some").total_us;
    previous_psi_full_total = memory_psi_values(options.cgroup_path, "full").total_us;

    output = fopen(options.output, "w");
    if (!output) {
        perror("output CSV");
        goto cleanup;
    }
    fprintf(output, "timestamp_ns,timestamp_monotonic_ns,sample_seq,run_id,app_id,app_name,operation_id,collector_backend,hook_type,hook_name,cgroup_path,cgroup_id,fault_total_delta,fault_total_rate,fault_anon_handler_delta,fault_anon_handler_rate,fault_file_handler_delta,fault_file_handler_rate,fault_wp_handler_delta,fault_wp_handler_rate,fault_shmem_handler_delta,fault_shmem_handler_rate,rss_process_sum_bytes,rss_process_count,rss_pid_read_failures,rss_read_cost_us,minor_fault_delta,major_fault_delta,memory_current_bytes,memory_anon_bytes,memory_file_bytes,mem_psi_some_avg10,mem_psi_full_avg10,mem_psi_some_total_us,mem_psi_full_total_us,mem_psi_some_total_delta_us,mem_psi_full_total_delta_us,mem_psi_some_ratio,mem_psi_full_ratio,sample_interval_ms,collector_read_cost_us,status,cgroup_match_mode,descendant_count,target_root_cgroup_id\n");
    fflush(output);

    info_length = sizeof(prog_info);
    if (bpf_obj_get_info_by_fd(program_fd, &prog_info, &info_length) != 0)
        memset(&prog_info, 0, sizeof(prog_info));
    info_length = sizeof(map_info);
    if (bpf_obj_get_info_by_fd(counters_fd, &map_info, &info_length) != 0)
        memset(&map_info, 0, sizeof(map_info));
    printf("collector_ready program_id=%u map_id=%u hook_type=%s hook_name=%s cgroup_id=%llu cgroup_path=%s cgroup_match=%s\n",
           prog_info.id, map_info.id, HOOK_TYPE, HOOK_NAME,
           (unsigned long long)cgroup_id, options.cgroup_path,
           options.match_mode == APP_CGROUP_MATCH_SUBTREE ? "subtree" : "exact");
    printf("handler_hooks anon=__vmf_anon_prepare file=filemap_fault wp=do_wp_page shmem=shmem_fault\n");
    fflush(stdout);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    start_ns = clock_ns(CLOCK_MONOTONIC);
    previous_ns = start_ns;
    next_ns = start_ns + (uint64_t)options.interval_ms * 1000000ULL;
    while (!stop_requested) {
        uint64_t read_start_ns;
        uint64_t now_ns;
        uint64_t wall_ns;
        struct app_fault_counters current_counters;
        uint64_t total_delta;
        uint64_t anon_delta;
        uint64_t file_delta;
        uint64_t wp_delta;
        uint64_t shmem_delta;
        uint64_t elapsed_ns;
        long long memory_current;
        long long memory_anon;
        long long memory_file;
        struct psi_values psi_some;
        struct psi_values psi_full;
        long long psi_some_delta;
        long long psi_full_delta;
        struct pid_list pids = {0};
        unsigned long long rss_pages = 0;
        unsigned long long rss_process_count = 0;
        unsigned long long rss_pid_read_failures = 0;
        long page_size_value;
        unsigned long long page_size;
        uint64_t rss_read_start_ns;
        uint64_t rss_read_end_ns;
        int rss_status;
        char memory_path[PATH_MAX];

        if (options.duration_seconds > 0 &&
            next_ns > start_ns + (uint64_t)options.duration_seconds * 1000000000ULL)
            break;
        if (sleep_until(next_ns) != 0) {
            perror("clock_nanosleep");
            goto cleanup;
        }
        if (stop_requested)
            break;
        read_start_ns = clock_ns(CLOCK_MONOTONIC);
        if (read_counter_totals(counters_fd, cpu_count, per_cpu,
                                &current_counters) != 0) {
            perror("counter map lookup");
            goto cleanup;
        }
        snprintf(memory_path, sizeof(memory_path), "%s/memory.current", options.cgroup_path);
        memory_current = read_integer_file(memory_path);
        memory_anon = memory_stat_value(options.cgroup_path, "anon");
        memory_file = memory_stat_value(options.cgroup_path, "file");
        psi_some = memory_psi_values(options.cgroup_path, "some");
        psi_full = memory_psi_values(options.cgroup_path, "full");
        rss_read_start_ns = clock_ns(CLOCK_MONOTONIC);
        rss_status = collect_subtree_pids(options.cgroup_path, &pids);
        page_size_value = sysconf(_SC_PAGESIZE);
        page_size = page_size_value > 0 ? (unsigned long long)page_size_value : 0;
        if (!page_size)
            rss_status = -1;
        for (size_t pid_index = 0; pid_index < pids.count; pid_index++) {
            unsigned long long resident_pages;

            if (read_proc_rss_pages(pids.values[pid_index], &resident_pages) != 0) {
                rss_pid_read_failures++;
                continue;
            }
            rss_pages += resident_pages;
            rss_process_count++;
        }
        if (rss_status != 0)
            rss_pid_read_failures++;
        rss_read_end_ns = clock_ns(CLOCK_MONOTONIC);
        free(pids.values);
        now_ns = clock_ns(CLOCK_MONOTONIC);
        wall_ns = clock_ns(CLOCK_REALTIME);
        total_delta = current_counters.fault_total >= previous_counters.fault_total ?
            current_counters.fault_total - previous_counters.fault_total : 0;
        anon_delta = current_counters.fault_anon_handler >= previous_counters.fault_anon_handler ?
            current_counters.fault_anon_handler - previous_counters.fault_anon_handler : 0;
        file_delta = current_counters.fault_file_handler >= previous_counters.fault_file_handler ?
            current_counters.fault_file_handler - previous_counters.fault_file_handler : 0;
        wp_delta = current_counters.fault_wp_handler >= previous_counters.fault_wp_handler ?
            current_counters.fault_wp_handler - previous_counters.fault_wp_handler : 0;
        shmem_delta = current_counters.fault_shmem_handler >= previous_counters.fault_shmem_handler ?
            current_counters.fault_shmem_handler - previous_counters.fault_shmem_handler : 0;
        elapsed_ns = now_ns - previous_ns;
        psi_some_delta = psi_some.total_us >= previous_psi_some_total &&
                         previous_psi_some_total >= 0 ?
                         psi_some.total_us - previous_psi_some_total : -1;
        psi_full_delta = psi_full.total_us >= previous_psi_full_total &&
                         previous_psi_full_total >= 0 ?
                         psi_full.total_us - previous_psi_full_total : -1;
        sequence++;

        fprintf(output, "%llu,%llu,%d,", (unsigned long long)wall_ns,
                (unsigned long long)now_ns, sequence);
        csv_string(output, options.run_id); fputc(',', output);
        csv_string(output, options.app_id); fputc(',', output);
        csv_string(output, options.app_name); fputc(',', output);
        csv_string(output, options.operation_id); fputc(',', output);
        fprintf(output, "ebpf,");
        csv_string(output, HOOK_TYPE); fputc(',', output);
        csv_string(output, HOOK_NAME); fputc(',', output);
        csv_string(output, options.cgroup_path);
        fprintf(output, ",%llu,%llu,%.6f,%llu,%.6f,%llu,%.6f,%llu,%.6f,%llu,%.6f,%llu,%llu,%llu,%.3f,,,",
                (unsigned long long)cgroup_id,
                (unsigned long long)total_delta,
                elapsed_ns ? (double)total_delta * 1000000000.0 / (double)elapsed_ns : 0.0,
                (unsigned long long)anon_delta,
                elapsed_ns ? (double)anon_delta * 1000000000.0 / (double)elapsed_ns : 0.0,
                (unsigned long long)file_delta,
                elapsed_ns ? (double)file_delta * 1000000000.0 / (double)elapsed_ns : 0.0,
                (unsigned long long)wp_delta,
                elapsed_ns ? (double)wp_delta * 1000000000.0 / (double)elapsed_ns : 0.0,
                (unsigned long long)shmem_delta,
                elapsed_ns ? (double)shmem_delta * 1000000000.0 / (double)elapsed_ns : 0.0,
                rss_pages * page_size, rss_process_count, rss_pid_read_failures,
                (double)(rss_read_end_ns - rss_read_start_ns) / 1000.0);
        if (memory_current >= 0) fprintf(output, "%lld", memory_current);
        fputc(',', output);
        if (memory_anon >= 0) fprintf(output, "%lld", memory_anon);
        fputc(',', output);
        if (memory_file >= 0) fprintf(output, "%lld", memory_file);
        fputc(',', output);
        if (psi_some.avg10 >= 0.0) fprintf(output, "%.2f", psi_some.avg10);
        fputc(',', output);
        if (psi_full.avg10 >= 0.0) fprintf(output, "%.2f", psi_full.avg10);
        fputc(',', output);
        if (psi_some.total_us >= 0) fprintf(output, "%lld", psi_some.total_us);
        fputc(',', output);
        if (psi_full.total_us >= 0) fprintf(output, "%lld", psi_full.total_us);
        fputc(',', output);
        if (psi_some_delta >= 0) fprintf(output, "%lld", psi_some_delta);
        fputc(',', output);
        if (psi_full_delta >= 0) fprintf(output, "%lld", psi_full_delta);
        fputc(',', output);
        if (psi_some_delta >= 0 && elapsed_ns)
            fprintf(output, "%.9f", (double)psi_some_delta * 1000.0 / (double)elapsed_ns);
        fputc(',', output);
        if (psi_full_delta >= 0 && elapsed_ns)
            fprintf(output, "%.9f", (double)psi_full_delta * 1000.0 / (double)elapsed_ns);
        fprintf(output, ",%.3f,%.3f,PASS,%s,%s,%llu\n",
                (double)elapsed_ns / 1000000.0,
                (double)(now_ns - read_start_ns) / 1000.0,
                options.match_mode == APP_CGROUP_MATCH_SUBTREE ? "subtree" : "exact",
                options.match_mode == APP_CGROUP_MATCH_SUBTREE ? "not_applicable" : "1",
                (unsigned long long)cgroup_id);
        fflush(output);
        previous_counters = current_counters;
        if (psi_some.total_us >= 0)
            previous_psi_some_total = psi_some.total_us;
        if (psi_full.total_us >= 0)
            previous_psi_full_total = psi_full.total_us;
        previous_ns = now_ns;
        next_ns += (uint64_t)options.interval_ms * 1000000ULL;
    }
    rc = 0;

cleanup:
    if (output)
        fclose(output);
    free(per_cpu);
    bpf_link__destroy(shmem_link);
    bpf_link__destroy(wp_link);
    bpf_link__destroy(file_link);
    bpf_link__destroy(anon_link);
    bpf_link__destroy(link);
    bpf_object__close(object);
    if (target_cgroup_fd >= 0)
        close(target_cgroup_fd);
    return rc;
}
