#include <uapi/linux/ptrace.h>
#include <linux/fdtable.h>
#include <linux/fs.h>
#include <linux/mm.h>
#include <linux/pagemap.h>
#include <linux/sched.h>

/*
 * 文件 syscall 的稳定事件编号。pread/pwrite 与普通 read/write 分开，是因为
 * 前两者的 offset 来自 syscall 参数，后两者的 offset 来自进入时的 file->f_pos。
 */
#define OP_OPENAT 1
#define OP_MMAP 2
#define OP_READ 3
#define OP_WRITE 4
#define OP_FSYNC 5
#define OP_RENAME 6
#define OP_CLOSE 7
#define OP_DUP 8
#define OP_PREAD 9
#define OP_PWRITE 10
#define OP_LSEEK 11
#define OP_ACCESS 12

/* 页缓存、调度和块层事件使用独立 perf buffer，避免大路径结构挤爆 BPF 栈。 */
#define CACHE_ACCESS 1
#define CACHE_EVICTION 2
#define CACHE_FILE_FAULT 3
#define CACHE_ADD 4
#define PAGE_SOURCE_DIRECT_ACCESS (1U << 1)
#define PAGE_SOURCE_FILE_FAULT (1U << 2)
#define WORKLOAD_PAGE_FAULT 1
#define WORKLOAD_BLOCK_IO 2
#define WORKLOAD_OFFCPU_SLEEP 3
#define WORKLOAD_OFFCPU_BLOCKED 4
#define WORKLOAD_IOWAIT 5

/*
 * eBPF 程序的栈上限是 512 字节。file_event_t 同时携带 rename 的两个路径及
 * 完整时序/文件身份字段，因此把单路径限制为 128 字节。极长路径会在用户态
 * 通过 path_truncated 计数显式暴露，不能被静默当成完整路径。
 */
#define PATH_LEN 128

struct file_id_t {
    u64 device;
    u64 inode;
};

struct inflight_t {
    u64 enter_boot_ns;
    u64 requested_size;
    s64 requested_offset;
    s64 entry_file_position;
    u64 inode;
    u64 device;
    u32 app_tag;
    u32 op;
    s32 fd;
    s32 dirfd;
    s32 dirfd2;
    u32 flags;
    u32 whence;
    u8 offset_valid;
    u8 file_identity_valid;
    char path[PATH_LEN];
    char path2[PATH_LEN];
};

struct file_event_t {
    u64 enter_boot_ns;
    u64 exit_boot_ns;
    u64 inode;
    s64 offset;
    s64 requested_offset;
    s64 file_position;
    u64 requested_size;
    u64 returned_size;
    s64 result;
    u64 device;
    u32 app_tag;
    u32 tgid;
    u32 tid;
    u32 uid;
    u32 op;
    s32 fd;
    s32 dirfd;
    s32 dirfd2;
    u32 flags;
    u32 whence;
    u8 offset_valid;
    u8 file_identity_valid;
    char comm[TASK_COMM_LEN];
    char path[PATH_LEN];
    char path2[PATH_LEN];
};

struct cache_event_t {
    u64 boot_timestamp_ns;
    u64 device;
    u64 inode;
    u64 offset;
    u64 size;
    u64 pfn;
    u32 app_tag;
    u32 tgid;
    u32 tid;
    u32 uid;
    u32 kind;
    u32 page_order;
    char comm[TASK_COMM_LEN];
};

struct workload_event_t {
    u64 boot_timestamp_ns;
    u64 value1;
    u64 value2;
    u64 value3;
    u64 device;
    u32 app_tag;
    u32 tgid;
    u32 tid;
    u32 uid;
    u32 kind;
    char comm[TASK_COMM_LEN];
    char rwbs[10];
};

struct offcpu_start_t {
    u64 start_boot_ns;
    u32 app_tag;
    u32 kind;
};

/*
 * page-access-window profile 不把每次 access 送到 perf ring。两个 Hash Map
 * 由用户态 helper 在窗口边界翻转：内核只在当前 epoch 内按文件页身份去重，
 * helper 则排空已经关闭的上一 epoch。first/last 时间仅用于边界审计，正式
 * 数据语义仍是“这个窗口内至少访问过一次”。
 */
struct page_window_key_t {
    u64 device;
    u64 inode;
    u64 page_index;
};

struct page_window_value_t {
    u64 first_boot_ns;
    u64 last_boot_ns;
    u64 pfn;
    u32 app_tag;
    u32 tgid;
    u32 tid;
    u32 source_mask;
};

/*
 * filemap tracepoint 偶尔用一条记录表示超过 512 页的读取范围。
 * 这些范围仍在内核的 epoch 双缓冲 Map 中去重，helper 只在每秒
 * 边界批量展开。这避免为一次大读取在 tracepoint 上执行数万次
 * Hash update，同时不会把“范围过大”误报为 Map 容量溢出。
 */
struct page_window_range_key_t {
    u64 device;
    u64 inode;
    u64 first_index;
    u64 last_index;
    u32 app_tag;
    u32 tgid;
    u32 tid;
};

struct page_window_range_value_t {
    u64 first_boot_ns;
    u64 last_boot_ns;
    u64 first_pfn;
    u32 source_mask;
};

struct file_fault_inflight_t {
    u64 vmf;
    u64 device;
    u64 inode;
    u64 page_index;
    u32 app_tag;
    u32 tgid;
    u32 tid;
};

/*
 * target_tgids/target_tids 的 value 是 helper 分配的稳定 app_tag，不只是布尔值。
 * 这样进程退出后发生的异步 page-cache eviction 仍能按 tag 归属到原 App。
 */
BPF_HASH(target_tgids, u32, u32, 16384);
BPF_HASH(target_tids, u32, u32, 65536);
BPF_HASH(inflight, u64, struct inflight_t, 32768);
BPF_HASH(offcpu_starts, u32, struct offcpu_start_t, 65536);
BPF_TABLE("lru_hash", struct file_id_t, u32, tracked_files, 131072);
BPF_TABLE("lru_hash", struct page_window_key_t, u64,
          page_identity_pfns, 262144);
BPF_PERCPU_ARRAY(file_event_scratch, struct file_event_t, 1);
/* 0=full telemetry; 1=dedicated page-hotset sampling.  This changes only
 * producer selection, not any emitted event ABI. 2=Page Idle window dataset. */
BPF_ARRAY(page_hotset_only, u32, 1);
BPF_HASH(page_capture_tags, u32, u8, 64);
BPF_HASH(page_window_epoch0, struct page_window_key_t,
         struct page_window_value_t, 65536);
BPF_HASH(page_window_epoch1, struct page_window_key_t,
         struct page_window_value_t, 65536);
BPF_HASH(page_window_ranges_epoch0, struct page_window_range_key_t,
         struct page_window_range_value_t, 8192);
BPF_HASH(page_window_ranges_epoch1, struct page_window_range_key_t,
         struct page_window_range_value_t, 8192);
BPF_ARRAY(page_window_epoch, u32, 1);
BPF_ARRAY(page_window_enabled, u32, 1);
BPF_ARRAY(page_window_overflows, u64, 2);
BPF_ARRAY(vmemmap_calibrated_base, u64, 1);
BPF_ARRAY(pfn_calibration_tgid, u32, 1);
BPF_ARRAY(pfn_calibration_device, u64, 1);
BPF_ARRAY(pfn_calibration_inode, u64, 1);
BPF_ARRAY(pfn_calibration_index, u64, 1);
BPF_ARRAY(pfn_calibration_folio, u64, 1);
BPF_ARRAY(kernel_page_struct_size, u64, 1);
BPF_HASH(page_mapping_dirty, u32, u8, 16384);
BPF_HASH(file_fault_inflight, u64, struct file_fault_inflight_t, 32768);
BPF_PERF_OUTPUT(events);
BPF_PERF_OUTPUT(cache_events);
BPF_PERF_OUTPUT(workload_events);

static __always_inline int is_page_hotset_only(void)
{
    u32 key = 0;
    u32 *enabled = page_hotset_only.lookup(&key);
    return enabled != 0 && *enabled == 1;
}

static __always_inline int is_page_access_window_only(void)
{
    u32 key = 0;
    u32 *enabled = page_hotset_only.lookup(&key);
    return enabled != 0 && *enabled == 2;
}

static __always_inline int is_compact_page_profile(void)
{
    return is_page_hotset_only() || is_page_access_window_only();
}

static __always_inline int capture_tag_enabled(u32 app_tag)
{
    u32 key = 0;
    u32 *enabled = page_window_enabled.lookup(&key);
    if (enabled == 0 || *enabled == 0)
        return 0;
    u8 *wanted = page_capture_tags.lookup(&app_tag);
    return wanted != 0 && *wanted != 0;
}

static __always_inline void note_page_window_overflow(u32 epoch)
{
    u32 key = epoch & 1;
    u64 *counter = page_window_overflows.lookup(&key);
    if (counter != 0)
        __sync_fetch_and_add(counter, 1);
}

static __always_inline int record_page_window(
    u64 device, u64 inode, u64 page_index, u64 pfn, u32 app_tag,
    u32 tgid, u32 tid, u32 source_mask)
{
    if (device == 0 || inode == 0 || !capture_tag_enabled(app_tag))
        return 0;
    u32 zero = 0;
    u32 *epoch_ptr = page_window_epoch.lookup(&zero);
    u32 epoch = epoch_ptr != 0 ? (*epoch_ptr & 1) : 0;
    struct page_window_key_t key = {
        .device = device,
        .inode = inode,
        .page_index = page_index,
    };
    struct page_window_value_t *existing = epoch
        ? page_window_epoch1.lookup(&key) : page_window_epoch0.lookup(&key);
    u64 now = bpf_ktime_get_ns();
    if (existing != 0) {
        existing->last_boot_ns = now;
        if (pfn != 0)
            existing->pfn = pfn;
        /* 同一文件页并发更新最多导致来源位稍后由另一 hook 再补齐；BCC/LLVM
         * 不支持对 map value 的 32 位 atomic-or，使用普通读改写保持可加载。 */
        existing->source_mask = existing->source_mask | source_mask;
        return 0;
    }
    struct page_window_value_t value = {
        .first_boot_ns = now,
        .last_boot_ns = now,
        .pfn = pfn,
        .app_tag = app_tag,
        .tgid = tgid,
        .tid = tid,
        .source_mask = source_mask,
    };
    int result = epoch
        ? page_window_epoch1.update(&key, &value)
        : page_window_epoch0.update(&key, &value);
    if (result != 0)
        note_page_window_overflow(epoch);
    return 0;
}

/*
 * mm_filemap_get_pages tracepoint 先于 folio_mark_accessed 执行，因此 direct
 * 记录第一次写入时可能还没有 PFN。folio hook 随后只更新“本 epoch 中已经
 * 存在”的页，不会把同一大 folio 中未被本次 read 覆盖的相邻页误报为访问。
 */
static __always_inline void update_page_window_pfn_if_present(
    u64 device, u64 inode, u64 page_index, u64 pfn)
{
    if (pfn == 0)
        return;
    u32 zero = 0;
    u32 *epoch_ptr = page_window_epoch.lookup(&zero);
    u32 epoch = epoch_ptr != 0 ? (*epoch_ptr & 1) : 0;
    struct page_window_key_t key = {
        .device = device,
        .inode = inode,
        .page_index = page_index,
    };
    struct page_window_value_t *existing = epoch
        ? page_window_epoch1.lookup(&key) : page_window_epoch0.lookup(&key);
    if (existing != 0)
        existing->pfn = pfn;
}

static __always_inline int record_page_window_range(
    u64 device, u64 inode, u64 first_index, u64 last_index,
    u64 first_pfn, u32 app_tag, u32 tgid, u32 tid, u32 source_mask)
{
    if (device == 0 || inode == 0 || app_tag == 0 || last_index < first_index)
        return 0;
    if (!capture_tag_enabled(app_tag))
        return 0;
    u32 zero = 0;
    u32 *enabled = page_window_enabled.lookup(&zero);
    if (enabled == 0 || *enabled == 0)
        return 0;
    u32 *epoch_ptr = page_window_epoch.lookup(&zero);
    u32 epoch = epoch_ptr != 0 ? (*epoch_ptr & 1) : 0;
    struct page_window_range_key_t key = {
        .device = device,
        .inode = inode,
        .first_index = first_index,
        .last_index = last_index,
        .app_tag = app_tag,
        .tgid = tgid,
        .tid = tid,
    };
    u64 now = bpf_ktime_get_ns();
    struct page_window_range_value_t *existing = epoch
        ? page_window_ranges_epoch1.lookup(&key)
        : page_window_ranges_epoch0.lookup(&key);
    if (existing != 0) {
        existing->last_boot_ns = now;
        if (first_pfn != 0)
            existing->first_pfn = first_pfn;
        existing->source_mask |= source_mask;
        return 0;
    }
    struct page_window_range_value_t value = {
        .first_boot_ns = now,
        .last_boot_ns = now,
        .first_pfn = first_pfn,
        .source_mask = source_mask,
    };
    int result = epoch
        ? page_window_ranges_epoch1.update(&key, &value)
        : page_window_ranges_epoch0.update(&key, &value);
    if (result != 0)
        note_page_window_overflow(epoch);
    return 0;
}

static __always_inline int mark_mapping_dirty(void)
{
    if (!is_page_access_window_only())
        return 0;
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *tag = target_tgids.lookup(&tgid);
    if (tag == 0 || !capture_tag_enabled(*tag))
        return 0;
    u8 one = 1;
    page_mapping_dirty.update(&tgid, &one);
    return 0;
}

/*
 * 在 syscall 边沿直接从当前任务 fdtable 取得普通文件的 device、inode 和 f_pos。
 * 这是事件时刻的内核对象身份，不依赖用户态稍后读取 /proc/<pid>/fd，因此不会
 * 因 close/dup 或路径改名把一个 syscall 错配到另一个文件。
 */
static __always_inline int snapshot_regular_file(
    s32 fd, u64 *device, u64 *inode_number, s64 *file_position)
{
    if (fd < 0)
        return 0;
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct files_struct *files = 0;
    struct fdtable *fdt = 0;
    struct file **fd_array = 0;
    struct file *file = 0;
    struct inode *inode = 0;
    struct super_block *super = 0;
    unsigned int max_fds = 0;
    umode_t mode = 0;

    bpf_probe_read_kernel(&files, sizeof(files), &task->files);
    if (files == 0)
        return 0;
    bpf_probe_read_kernel(&fdt, sizeof(fdt), &files->fdt);
    if (fdt == 0)
        return 0;
    bpf_probe_read_kernel(&max_fds, sizeof(max_fds), &fdt->max_fds);
    if ((u32)fd >= max_fds)
        return 0;
    bpf_probe_read_kernel(&fd_array, sizeof(fd_array), &fdt->fd);
    if (fd_array == 0)
        return 0;
    bpf_probe_read_kernel(&file, sizeof(file), &fd_array[fd]);
    if (file == 0)
        return 0;
    bpf_probe_read_kernel(&inode, sizeof(inode), &file->f_inode);
    if (inode == 0)
        return 0;
    bpf_probe_read_kernel(&mode, sizeof(mode), &inode->i_mode);
    if ((mode & S_IFMT) != S_IFREG)
        return 0;
    bpf_probe_read_kernel(inode_number, sizeof(*inode_number), &inode->i_ino);
    bpf_probe_read_kernel(&super, sizeof(super), &inode->i_sb);
    if (super == 0 || *inode_number == 0)
        return 0;
    dev_t dev = 0;
    bpf_probe_read_kernel(&dev, sizeof(dev), &super->s_dev);
    *device = (u64)dev;
    bpf_probe_read_kernel(file_position, sizeof(*file_position), &file->f_pos);
    return 1;
}

static __always_inline void remember_file(
    u64 device, u64 inode, u32 app_tag)
{
    if (device == 0 || inode == 0 || app_tag == 0)
        return;
    struct file_id_t key = {.device = device, .inode = inode};
    tracked_files.update(&key, &app_tag);
}

/*
 * readFile 是用户要求的内核侧 read hook。每次成功或失败的 read/pread 返回都
 * 会进入这里；成功事件同时把文件身份记到 tracked_files，供稍后的异步 eviction
 * 归属使用。perf_submit 是内核到用户态的单向批量缓冲，不存在一次事件一次 RPC。
 */
static __always_inline int readFile(void *ctx, struct file_event_t *event)
{
    if (is_compact_page_profile())
        return 0;
    if (event->file_identity_valid)
        remember_file(event->device, event->inode, event->app_tag);
    events.perf_submit(ctx, event, sizeof(*event));
    return 0;
}

/* mm_filemap_get_pages 的每次触发都在内核直接调用 accessFile。 */
static __always_inline int accessFile(
    void *ctx, u64 device, u64 inode, u64 first_index, u64 last_index)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *app_tag = target_tgids.lookup(&tgid);
    if (app_tag == 0 || *app_tag == 0 || inode == 0)
        return 0;
    struct cache_event_t event = {};
    u64 uid_gid = bpf_get_current_uid_gid();
    event.boot_timestamp_ns = bpf_ktime_get_ns();
    event.device = device;
    event.inode = inode;
    event.offset = first_index << PAGE_SHIFT;
    event.size = last_index >= first_index
        ? (last_index - first_index + 1) << PAGE_SHIFT : 0;
    event.app_tag = *app_tag;
    event.tgid = tgid;
    event.tid = (u32)id;
    event.uid = (u32)uid_gid;
    event.kind = CACHE_ACCESS;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    remember_file(device, inode, *app_tag);
    if (is_page_access_window_only()) {
        /* procfs/sysfs/tmpfs 等 major=0 的 regular inode 不提供本数据集所需
         * 的普通文件页物理身份；匿名/shmem 页也明确不在采集范围。 */
        if ((device >> 20) == 0)
            return 0;
        /*
         * get_pages 给出本次 buffered I/O 真正取得的文件页范围。按页写入
         * 内核 Hash Map 后，同一秒重复读同一页仍只有一条记录。为保证 BPF
         * 程序有严格上界，单次最多展开 512 页；更大范围会递增 overflow，
         * 对应窗口因此被判为 INVALID，绝不静默截断。
         */
        u32 zero = 0;
        u32 *epoch_ptr = page_window_epoch.lookup(&zero);
        u32 epoch = epoch_ptr != 0 ? (*epoch_ptr & 1) : 0;
        if (last_index >= first_index && last_index - first_index >= 512) {
            return record_page_window_range(
                device, inode, first_index, last_index, 0, *app_tag,
                tgid, (u32)id, PAGE_SOURCE_DIRECT_ACCESS);
        }
        for (u32 delta = 0; delta < 512; delta++) {
            u64 page_index = first_index + delta;
            if (page_index < first_index || page_index > last_index)
                break;
            struct page_window_key_t page_key = {
                .device = device,
                .inode = inode,
                .page_index = page_index,
            };
            u64 pfn = 0;
            u64 *known_pfn = page_identity_pfns.lookup(&page_key);
            if (known_pfn != 0)
                pfn = *known_pfn;
            record_page_window(
                device, inode, page_index, pfn, *app_tag,
                tgid, (u32)id, PAGE_SOURCE_DIRECT_ACCESS);
        }
        return 0;
    }
    cache_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

/*
 * mm_filemap_delete_from_page_cache 可能在 kswapd/writeback 等内核线程中执行，
 * 所以 evictFile 不能用“当前 PID”冒充归属。它按 device+inode 查询最近访问该
 * 文件的已定义 App tag，并把回收线程的真实 tid/comm 作为执行上下文一并上报。
 */
static __always_inline int evictFile(
    void *ctx, u64 device, u64 inode, u64 page_index, u64 pfn, u32 page_order)
{
    u64 page_count = page_order < 63 ? (1ULL << page_order) : 0;
    for (u32 delta = 0; delta < 512; delta++) {
        if ((u64)delta >= page_count)
            break;
        struct page_window_key_t page_key = {
            .device = device,
            .inode = inode,
            .page_index = page_index + delta,
        };
        u64 *known_pfn = page_identity_pfns.lookup(&page_key);
        if (known_pfn != 0 && *known_pfn == pfn + delta)
            page_identity_pfns.delete(&page_key);
    }
    if (is_page_hotset_only())
        return 0;
    struct file_id_t key = {.device = device, .inode = inode};
    u32 *app_tag = tracked_files.lookup(&key);
    if (app_tag == 0 || *app_tag == 0)
        return 0;
    u64 id = bpf_get_current_pid_tgid();
    struct cache_event_t event = {};
    u64 uid_gid = bpf_get_current_uid_gid();
    event.boot_timestamp_ns = bpf_ktime_get_ns();
    event.device = device;
    event.inode = inode;
    event.offset = page_index << PAGE_SHIFT;
    event.size = (1ULL << page_order) << PAGE_SHIFT;
    event.pfn = pfn;
    event.app_tag = *app_tag;
    event.tgid = id >> 32;
    event.tid = (u32)id;
    event.uid = (u32)uid_gid;
    event.kind = CACHE_EVICTION;
    event.page_order = page_order;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    cache_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

static __always_inline int begin_event(
    u32 op, s32 fd, s32 dirfd, s32 dirfd2, u64 requested_size,
    s64 requested_offset, u8 explicit_offset, u32 flags, u32 whence,
    const char *path, const char *path2)
{
    /* page-access-window 仍需在 read/pread 出口取得“实际返回字节数”；
     * 其他 syscall 以及旧 page-hotset profile 保持零 inflight 开销。 */
    if (is_page_hotset_only() ||
        (is_page_access_window_only() && op != OP_READ && op != OP_PREAD))
        return 0;
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *app_tag = target_tgids.lookup(&tgid);
    if (app_tag == 0 || *app_tag == 0)
        return 0;
    struct inflight_t item = {};
    item.enter_boot_ns = bpf_ktime_get_ns();
    item.requested_size = requested_size;
    item.requested_offset = requested_offset;
    item.app_tag = *app_tag;
    item.op = op;
    item.fd = fd;
    item.dirfd = dirfd;
    item.dirfd2 = dirfd2;
    item.flags = flags;
    item.whence = whence;
    item.offset_valid = explicit_offset;
    item.file_identity_valid = snapshot_regular_file(
        fd, &item.device, &item.inode, &item.entry_file_position);
    if (!explicit_offset && item.file_identity_valid &&
        (op == OP_READ || op == OP_WRITE)) {
        item.requested_offset = item.entry_file_position;
        item.offset_valid = 1;
    }
    if (path != 0)
        bpf_probe_read_user_str(item.path, sizeof(item.path), path);
    if (path2 != 0)
        bpf_probe_read_user_str(item.path2, sizeof(item.path2), path2);
    inflight.update(&id, &item);
    return 0;
}

static __always_inline int finish_event(void *ctx, s64 result)
{
    u64 id = bpf_get_current_pid_tgid();
    struct inflight_t *item = inflight.lookup(&id);
    if (item == 0)
        return 0;
    u32 scratch_key = 0;
    struct file_event_t *event = file_event_scratch.lookup(&scratch_key);
    if (event == 0) {
        inflight.delete(&id);
        return 0;
    }
    __builtin_memset(event, 0, sizeof(*event));
    u64 uid_gid = bpf_get_current_uid_gid();
    event->enter_boot_ns = item->enter_boot_ns;
    event->exit_boot_ns = bpf_ktime_get_ns();
    event->inode = item->inode;
    event->offset = item->requested_offset;
    event->requested_offset = item->requested_offset;
    event->file_position = item->entry_file_position;
    event->requested_size = item->requested_size;
    event->returned_size = result > 0 &&
        (item->op == OP_READ || item->op == OP_PREAD ||
         item->op == OP_WRITE || item->op == OP_PWRITE)
        ? (u64)result : 0;
    event->result = result;
    event->device = item->device;
    event->app_tag = item->app_tag;
    event->tgid = id >> 32;
    event->tid = (u32)id;
    event->uid = (u32)uid_gid;
    event->op = item->op;
    event->fd = item->op == OP_OPENAT && result >= 0 ? (s32)result : item->fd;
    event->dirfd = item->dirfd;
    event->dirfd2 = item->dirfd2;
    event->flags = item->flags;
    event->whence = item->whence;
    event->offset_valid = item->offset_valid;
    event->file_identity_valid = item->file_identity_valid;

    /* open 返回后 fd 才存在；在退出边沿补取精确文件身份。 */
    if (item->op == OP_OPENAT && result >= 0) {
        event->file_identity_valid = snapshot_regular_file(
            (s32)result, &event->device, &event->inode, &event->file_position);
    } else if (item->fd >= 0) {
        u64 exit_device = 0;
        u64 exit_inode = 0;
        s64 exit_position = 0;
        if (snapshot_regular_file(
                item->fd, &exit_device, &exit_inode, &exit_position)) {
            event->file_position = exit_position;
            if (!event->file_identity_valid) {
                event->file_identity_valid = 1;
                event->device = exit_device;
                event->inode = exit_inode;
            }
        }
    }
    if (item->op == OP_LSEEK && result >= 0) {
        event->offset = result;
        event->offset_valid = 1;
    }
    bpf_get_current_comm(&event->comm, sizeof(event->comm));
    __builtin_memcpy(event->path, item->path, sizeof(event->path));
    __builtin_memcpy(event->path2, item->path2, sizeof(event->path2));

    if (item->op == OP_READ || item->op == OP_PREAD) {
        if (is_page_access_window_only() && event->file_identity_valid &&
            event->offset_valid && event->returned_size > 0) {
            /* 使用 syscall 的真实返回长度，而不是 get_pages 的请求上界；
             * 后者在短文件/EOF 上会包含根本不存在的尾页。 */
            u64 first_index = ((u64)event->offset) >> PAGE_SHIFT;
            u64 last_byte = (u64)event->offset + event->returned_size - 1;
            if (last_byte >= (u64)event->offset)
                accessFile(
                    ctx, event->device, event->inode,
                    first_index, last_byte >> PAGE_SHIFT);
        } else {
            readFile(ctx, event);
        }
    }
    else {
        if (event->file_identity_valid &&
            (item->op == OP_WRITE || item->op == OP_PWRITE ||
             item->op == OP_MMAP))
            remember_file(event->device, event->inode, event->app_tag);
        events.perf_submit(ctx, event, sizeof(*event));
    }
    inflight.delete(&id);
    return 0;
}

TRACEPOINT_PROBE(syscalls, sys_enter_openat)
{
    return begin_event(OP_OPENAT, -1, args->dfd, -1, 0, 0, 0,
                       args->flags, 0, args->filename, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_openat) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_openat2)
{
    return begin_event(OP_OPENAT, -1, args->dfd, -1, 0, 0, 0,
                       0, 0, args->filename, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_openat2) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_mmap)
{
    mark_mapping_dirty();
    return begin_event(OP_MMAP, (s32)args->fd, -1, -1, args->len,
                       (s64)args->off, 1, args->flags, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_mmap) { return finish_event(args, args->ret); }

/* mmap/munmap/mremap 只把目标 PID 标为 dirty；helper 在下一窗口边界定向重扫。 */
TRACEPOINT_PROBE(syscalls, sys_enter_munmap)
{
    return mark_mapping_dirty();
}

TRACEPOINT_PROBE(syscalls, sys_enter_mremap)
{
    return mark_mapping_dirty();
}

TRACEPOINT_PROBE(syscalls, sys_enter_read)
{
    return begin_event(OP_READ, (s32)args->fd, -1, -1, args->count,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_read) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_pread64)
{
    return begin_event(OP_PREAD, (s32)args->fd, -1, -1, args->count,
                       (s64)args->pos, 1, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_pread64) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_write)
{
    return begin_event(OP_WRITE, (s32)args->fd, -1, -1, args->count,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_write) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_pwrite64)
{
    return begin_event(OP_PWRITE, (s32)args->fd, -1, -1, args->count,
                       (s64)args->pos, 1, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_pwrite64) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_lseek)
{
    return begin_event(OP_LSEEK, (s32)args->fd, -1, -1, 0,
                       (s64)args->offset, 1, 0, args->whence, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_lseek) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_fsync)
{
    return begin_event(OP_FSYNC, (s32)args->fd, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_fsync) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_fdatasync)
{
    return begin_event(OP_FSYNC, (s32)args->fd, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_fdatasync) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_access)
{
    return begin_event(OP_ACCESS, -1, -100, -1, 0, 0, 0,
                       args->mode, 0, args->filename, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_access) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_faccessat)
{
    return begin_event(OP_ACCESS, -1, args->dfd, -1, 0, 0, 0,
                       args->mode, 0, args->filename, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_faccessat) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_faccessat2)
{
    return begin_event(OP_ACCESS, -1, args->dfd, -1, 0, 0, 0,
                       args->flags, args->mode, args->filename, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_faccessat2) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_rename)
{
    return begin_event(OP_RENAME, -1, -100, -100, 0, 0, 0,
                       0, 0, args->oldname, args->newname);
}
TRACEPOINT_PROBE(syscalls, sys_exit_rename) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_renameat)
{
    return begin_event(OP_RENAME, -1, args->olddfd, args->newdfd, 0, 0, 0,
                       0, 0, args->oldname, args->newname);
}
TRACEPOINT_PROBE(syscalls, sys_exit_renameat) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_renameat2)
{
    return begin_event(OP_RENAME, -1, args->olddfd, args->newdfd, 0, 0, 0,
                       args->flags, 0, args->oldname, args->newname);
}
TRACEPOINT_PROBE(syscalls, sys_exit_renameat2) { return finish_event(args, args->ret); }

/* close/dup 只维护用户态 fd->path 缓存，不进入 App 的文件工作负载计数。 */
TRACEPOINT_PROBE(syscalls, sys_enter_close)
{
    return begin_event(OP_CLOSE, (s32)args->fd, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_close) { return finish_event(args, args->ret); }

TRACEPOINT_PROBE(syscalls, sys_enter_dup)
{
    return begin_event(OP_DUP, (s32)args->fildes, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_dup) { return finish_event(args, args->ret); }
TRACEPOINT_PROBE(syscalls, sys_enter_dup2)
{
    return begin_event(OP_DUP, (s32)args->oldfd, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_dup2) { return finish_event(args, args->ret); }
TRACEPOINT_PROBE(syscalls, sys_enter_dup3)
{
    return begin_event(OP_DUP, (s32)args->oldfd, -1, -1, 0,
                       0, 0, 0, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_dup3) { return finish_event(args, args->ret); }

/* 文件页实际进入 page-cache 读取路径；一条事件给出完整 page-index 范围。 */
TRACEPOINT_PROBE(filemap, mm_filemap_get_pages)
{
    /* page-access-window 在 read/pread 返回边沿按实际返回字节记录；本
     * tracepoint 的 last_index 是请求上界，短读/EOF 时并非实际访问页。 */
    if (is_page_access_window_only())
        return 0;
    return accessFile(args, args->s_dev, args->i_ino, args->index, args->last_index);
}

/*
 * 从 folio 只读取文件页稳定键。mapping 低位为 PAGE_MAPPING_ANON 时
 * 立即排除，因此匿名页不会进入该数据集。
 */
static __always_inline int file_folio_identity(
    struct folio *folio, u64 *device, u64 *inode_number, u64 *page_index)
{
    if (folio == 0)
        return 0;
    struct address_space *raw_mapping = 0;
    bpf_probe_read_kernel(&raw_mapping, sizeof(raw_mapping), &folio->mapping);
    unsigned long mapping_value = (unsigned long)raw_mapping;
    if (mapping_value == 0 || (mapping_value & 1UL))
        return 0;
    struct address_space *mapping = (struct address_space *)(mapping_value & ~3UL);
    struct inode *inode = 0;
    bpf_probe_read_kernel(&inode, sizeof(inode), &mapping->host);
    if (inode == 0)
        return 0;
    umode_t mode = 0;
    bpf_probe_read_kernel(&mode, sizeof(mode), &inode->i_mode);
    if ((mode & S_IFMT) != S_IFREG)
        return 0;
    struct super_block *super = 0;
    pgoff_t index = 0;
    bpf_probe_read_kernel(inode_number, sizeof(*inode_number), &inode->i_ino);
    bpf_probe_read_kernel(&super, sizeof(super), &inode->i_sb);
    bpf_probe_read_kernel(&index, sizeof(index), &folio->index);
    if (super == 0 || *inode_number == 0)
        return 0;
    dev_t raw_device = 0;
    bpf_probe_read_kernel(&raw_device, sizeof(raw_device), &super->s_dev);
    *device = (u64)raw_device;
    *page_index = (u64)index;
    return 1;
}

/* vmemmap 基址已经由 helper 双页交叉校准；这里只做整数换算，不读 kallsyms。 */
static __always_inline u64 page_pointer_to_pfn(struct page *page)
{
    u32 zero = 0;
    u64 *vmemmap_base = vmemmap_calibrated_base.lookup(&zero);
    if (page == 0 || vmemmap_base == 0 || *vmemmap_base == 0 ||
        (u64)page < *vmemmap_base)
        return 0;
    u64 distance = (u64)page - *vmemmap_base;
    if (distance % sizeof(struct page) != 0)
        return 0;
    return distance / sizeof(struct page);
}

/* 在 filemap_fault 入口保存稳定文件页身份，返回时再读取成功安装的 vmf->page。 */
int kprobe__filemap_fault(struct pt_regs *ctx, struct vm_fault *vmf)
{
    if (!is_page_access_window_only() || vmf == 0)
        return 0;
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *app_tag = target_tgids.lookup(&tgid);
    if (app_tag == 0 || !capture_tag_enabled(*app_tag))
        return 0;
    struct vm_area_struct *vma = 0;
    struct file *file = 0;
    struct address_space *mapping = 0;
    struct inode *inode = 0;
    struct super_block *super = 0;
    pgoff_t index = 0;
    u64 inode_number = 0;
    dev_t raw_device = 0;
    bpf_probe_read_kernel(&vma, sizeof(vma), &vmf->vma);
    if (vma == 0)
        return 0;
    bpf_probe_read_kernel(&file, sizeof(file), &vma->vm_file);
    if (file == 0)
        return 0;
    bpf_probe_read_kernel(&mapping, sizeof(mapping), &file->f_mapping);
    if (mapping == 0)
        return 0;
    bpf_probe_read_kernel(&inode, sizeof(inode), &mapping->host);
    if (inode == 0)
        return 0;
    bpf_probe_read_kernel(&super, sizeof(super), &inode->i_sb);
    bpf_probe_read_kernel(&inode_number, sizeof(inode_number), &inode->i_ino);
    bpf_probe_read_kernel(&index, sizeof(index), &vmf->pgoff);
    if (super == 0 || inode_number == 0)
        return 0;
    bpf_probe_read_kernel(&raw_device, sizeof(raw_device), &super->s_dev);
    if (((u64)raw_device >> 20) == 0)
        return 0;
    struct file_fault_inflight_t state = {
        .vmf = (u64)vmf,
        .device = (u64)raw_device,
        .inode = inode_number,
        .page_index = (u64)index,
        .app_tag = *app_tag,
        .tgid = tgid,
        .tid = (u32)id,
    };
    file_fault_inflight.update(&id, &state);
    return 0;
}

int kretprobe__filemap_fault(struct pt_regs *ctx)
{
    u64 id = bpf_get_current_pid_tgid();
    struct file_fault_inflight_t *state = file_fault_inflight.lookup(&id);
    if (state == 0)
        return 0;
    struct vm_fault *vmf = (struct vm_fault *)state->vmf;
    struct page *page = 0;
    bpf_probe_read_kernel(&page, sizeof(page), &vmf->page);
    u64 pfn = page_pointer_to_pfn(page);
    if (pfn != 0 && (PT_REGS_RC(ctx) & VM_FAULT_LOCKED)) {
        struct page_window_key_t key = {
            .device = state->device,
            .inode = state->inode,
            .page_index = state->page_index,
        };
        page_identity_pfns.update(&key, &pfn);
        record_page_window(
            state->device, state->inode, state->page_index, pfn,
            state->app_tag, state->tgid, state->tid,
            PAGE_SOURCE_FILE_FAULT);
        remember_file(state->device, state->inode, state->app_tag);

        struct cache_event_t event = {};
        event.boot_timestamp_ns = bpf_ktime_get_ns();
        event.device = state->device;
        event.inode = state->inode;
        event.offset = state->page_index << PAGE_SHIFT;
        event.size = PAGE_SIZE;
        event.pfn = pfn;
        event.app_tag = state->app_tag;
        event.tgid = state->tgid;
        event.tid = state->tid;
        event.uid = (u32)bpf_get_current_uid_gid();
        event.kind = CACHE_FILE_FAULT;
        event.page_order = 0;
        bpf_get_current_comm(&event.comm, sizeof(event.comm));
        cache_events.perf_submit(ctx, &event, sizeof(event));
    }
    file_fault_inflight.delete(&id);
    return 0;
}

/*
 * buffered read 真正引用每个 folio 时会调用该内核函数。与只提供
 * page-index 范围的 filemap tracepoint 结合，这个 hook 可以在内核内补齐
 * 当次 PFN。vmemmap 基址由 helper 用两个受控临时文件页的
 * ``pagemap PFN + folio 指针`` 交叉校验得到，不依赖 kallsyms。
 */
int kprobe__folio_mark_accessed_native(
    struct pt_regs *ctx, struct folio *folio)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 zero = 0;
    u64 device = 0;
    u64 inode = 0;
    u64 page_index = 0;
    if (!file_folio_identity(folio, &device, &inode, &page_index))
        return 0;

    /* calibration 分支只接受 helper 指定的 TGID+文件页四元组。 */
    u32 *calibration_tgid = pfn_calibration_tgid.lookup(&zero);
    if (calibration_tgid != 0 && *calibration_tgid == tgid) {
        u64 *wanted_device = pfn_calibration_device.lookup(&zero);
        u64 *wanted_inode = pfn_calibration_inode.lookup(&zero);
        u64 *wanted_index = pfn_calibration_index.lookup(&zero);
        if (wanted_device != 0 && wanted_inode != 0 && wanted_index != 0 &&
            device == *wanted_device && inode == *wanted_inode &&
            page_index == *wanted_index) {
            u64 folio_address = (u64)folio;
            u64 page_struct_size = sizeof(struct page);
            pfn_calibration_folio.update(&zero, &folio_address);
            kernel_page_struct_size.update(&zero, &page_struct_size);
        }
        return 0;
    }

    if (!is_page_access_window_only())
        return 0;
    u32 *app_tag = target_tgids.lookup(&tgid);
    if (app_tag == 0 || !capture_tag_enabled(*app_tag))
        return 0;
    u64 pfn = page_pointer_to_pfn(&folio->page);
    if (pfn == 0)
        return 0;
    /*
     * 大 folio 中的每个 4 KiB 页拥有连续 page_index/PFN。这里只
     * 补物理身份，“哪些子页真正被读取”仍由
     * mm_filemap_get_pages 的精确请求范围决定，避免把同 folio
     * 中未读的相邻页误标成访问。
     */
    /*
     * 不能在 kprobe 中直接调用 folio_nr_pages(folio)：BCC 会把其中的
     * ``folio->flags`` / ``folio->_nr_pages`` 普通指针解引用展开进 BPF，
     * verifier 只把 kprobe 参数视为不可信的 scalar，因而会拒绝程序。
     * 这里显式使用 probe_read_kernel 安全读取。当前内核启用了 MEMCG，
     * 大 folio 的实际页数保存在 _nr_pages；普通 folio 固定为一页。
     */
    unsigned long folio_flags = 0;
    u32 large_folio_pages = 1;
    u64 folio_pages = 1;
    bpf_probe_read_kernel(&folio_flags, sizeof(folio_flags), &folio->flags);
    if (folio_flags & (1UL << PG_head)) {
        bpf_probe_read_kernel(
            &large_folio_pages, sizeof(large_folio_pages),
            &folio->_nr_pages);
        /* 文件页 folio 不应超过 512 个 4 KiB 页；同时给循环明确上界。 */
        if (large_folio_pages > 1 && large_folio_pages <= 512)
            folio_pages = large_folio_pages;
    }
    for (u32 delta = 0; delta < 512; delta++) {
        if ((u64)delta >= folio_pages)
            break;
        struct page_window_key_t key = {
            .device = device,
            .inode = inode,
            .page_index = page_index + delta,
        };
        u64 child_pfn = pfn + delta;
        page_identity_pfns.update(&key, &child_pfn);
        update_page_window_pfn_if_present(
            device, inode, page_index + delta, child_pfn);
    }
    remember_file(device, inode, *app_tag);
    return 0;
}

/* 保留 tracepoint ABI；精确 FAULT/PFN 由 filemap_fault kretprobe 成功返回时提交。 */
TRACEPOINT_PROBE(filemap, mm_filemap_fault)
{
    return 0;
}

/* 加入 page cache 只维护 PFN 身份，不直接把 readahead 页判为已访问。 */
TRACEPOINT_PROBE(filemap, mm_filemap_add_to_page_cache)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *app_tag = target_tgids.lookup(&tgid);
    if (app_tag == 0)
        return 0;
    if (!is_page_access_window_only() || !capture_tag_enabled(*app_tag))
        return 0;
    u64 page_count = args->order < 63 ? (1ULL << args->order) : 0;
    for (u32 delta = 0; delta < 512; delta++) {
        if ((u64)delta >= page_count)
            break;
        struct page_window_key_t page_key = {
            .device = args->s_dev,
            .inode = args->i_ino,
            .page_index = args->index + delta,
        };
        u64 pfn = args->pfn + delta;
        page_identity_pfns.update(&page_key, &pfn);
    }
    if (page_count > 512) {
        u32 zero = 0;
        u32 *epoch_ptr = page_window_epoch.lookup(&zero);
        note_page_window_overflow(epoch_ptr != 0 ? (*epoch_ptr & 1) : 0);
    }
    remember_file(args->s_dev, args->i_ino, *app_tag);
    struct cache_event_t event = {};
    event.boot_timestamp_ns = bpf_ktime_get_ns();
    event.device = args->s_dev;
    event.inode = args->i_ino;
    event.offset = args->index << PAGE_SHIFT;
    event.size = (1ULL << args->order) << PAGE_SHIFT;
    event.pfn = args->pfn;
    event.app_tag = *app_tag;
    event.tgid = tgid;
    event.tid = (u32)id;
    event.uid = (u32)bpf_get_current_uid_gid();
    event.kind = CACHE_ADD;
    event.page_order = args->order;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    cache_events.perf_submit(args, &event, sizeof(event));
    return 0;
}

/* 文件页真正从 page cache 删除；不是“内存下降”的周期推断。 */
TRACEPOINT_PROBE(filemap, mm_filemap_delete_from_page_cache)
{
    return evictFile(
        args, args->s_dev, args->i_ino, args->index, args->pfn, args->order);
}

static __always_inline int submit_workload(
    void *ctx, u32 kind, u32 app_tag, u32 tgid, u32 tid,
    u64 value1, u64 value2, u64 value3, u64 device,
    const char *rwbs)
{
    if (is_compact_page_profile())
        return 0;
    if (app_tag == 0)
        return 0;
    struct workload_event_t event = {};
    u64 uid_gid = bpf_get_current_uid_gid();
    event.boot_timestamp_ns = bpf_ktime_get_ns();
    event.value1 = value1;
    event.value2 = value2;
    event.value3 = value3;
    event.device = device;
    event.app_tag = app_tag;
    event.tgid = tgid;
    event.tid = tid;
    event.uid = (u32)uid_gid;
    event.kind = kind;
    bpf_get_current_comm(&event.comm, sizeof(event.comm));
    if (rwbs != 0)
        bpf_probe_read_kernel(&event.rwbs, sizeof(event.rwbs), rwbs);
    workload_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

TRACEPOINT_PROBE(exceptions, page_fault_user)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *tag = target_tgids.lookup(&tgid);
    if (tag == 0)
        return 0;
    return submit_workload(args, WORKLOAD_PAGE_FAULT, *tag, tgid, (u32)id,
                           args->address, args->ip, args->error_code, 0, 0);
}

/* 只归属在目标 App 上下文中直接签发的 block request；异步 writeback 另行标注。 */
TRACEPOINT_PROBE(block, block_rq_issue)
{
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 *tag = target_tgids.lookup(&tgid);
    if (tag == 0)
        return 0;
    return submit_workload(args, WORKLOAD_BLOCK_IO, *tag, tgid, (u32)id,
                           args->sector, args->nr_sector, args->bytes,
                           args->dev, args->rwbs);
}

static __always_inline int submit_sched_delay(void *ctx, u32 kind, u32 tid, u64 delay)
{
    if (is_compact_page_profile())
        return 0;
    u32 *tag = target_tids.lookup(&tid);
    if (tag == 0)
        return 0;
    /* sched_stat tracepoint不提供 TGID；helper 通过 tag 归属，tid 保留真实线程。 */
    return submit_workload(ctx, kind, *tag, 0, tid, delay, 0, 0, 0, 0);
}

/*
 * sched_switch 在 kernel.sched_schedstats=0 时仍始终可用。目标线程换出时只在
 * 内核 map 记时间，重新换入时计算真实 off-CPU 区间并上报一次；D 状态单列为
 * blocked，其余睡眠/可运行但未获 CPU 的时间归入 sleep/general off-CPU。
 */
TRACEPOINT_PROBE(sched, sched_switch)
{
    if (is_page_hotset_only())
        return 0;
    u32 prev_tid = args->prev_pid;
    u32 next_tid = args->next_pid;
    u64 now = bpf_ktime_get_ns();
    u32 *prev_tag = target_tids.lookup(&prev_tid);
    if (prev_tag != 0) {
        struct offcpu_start_t start = {};
        start.start_boot_ns = now;
        start.app_tag = *prev_tag;
        start.kind = (args->prev_state & 0x2)
            ? WORKLOAD_OFFCPU_BLOCKED : WORKLOAD_OFFCPU_SLEEP;
        offcpu_starts.update(&prev_tid, &start);
    }
    struct offcpu_start_t *start = offcpu_starts.lookup(&next_tid);
    if (start != 0) {
        u64 delay = now > start->start_boot_ns ? now - start->start_boot_ns : 0;
        submit_workload(args, start->kind, start->app_tag, 0, next_tid,
                        delay, 0, 0, 0, 0);
        offcpu_starts.delete(&next_tid);
    }
    return 0;
}

/* schedstats 开启时额外给出内核明确标记的 iowait；默认关闭时该列自然为 0。 */
TRACEPOINT_PROBE(sched, sched_stat_iowait)
{
    return submit_sched_delay(args, WORKLOAD_IOWAIT, args->pid, args->delay);
}

/* 新线程继承父线程的 App tag，避免必须等下一次用户态 PID 快照才能统计等待。 */
TRACEPOINT_PROBE(sched, sched_process_fork)
{
    if (is_page_hotset_only())
        return 0;
    u32 parent_tid = args->parent_pid;
    u32 child_tid = args->child_pid;
    u32 *tag = target_tids.lookup(&parent_tid);
    if (tag != 0)
        target_tids.update(&child_tid, tag);
    return 0;
}
TRACEPOINT_PROBE(sched, sched_process_exit)
{
    if (is_page_hotset_only())
        return 0;
    u32 tid = (u32)bpf_get_current_pid_tgid();
    target_tids.delete(&tid);
    return 0;
}
