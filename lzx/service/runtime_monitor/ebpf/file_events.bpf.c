#include <uapi/linux/ptrace.h>
#include <linux/fdtable.h>
#include <linux/fs.h>
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
 * target_tgids/target_tids 的 value 是 helper 分配的稳定 app_tag，不只是布尔值。
 * 这样进程退出后发生的异步 page-cache eviction 仍能按 tag 归属到原 App。
 */
BPF_HASH(target_tgids, u32, u32, 16384);
BPF_HASH(target_tids, u32, u32, 65536);
BPF_HASH(inflight, u64, struct inflight_t, 32768);
BPF_HASH(offcpu_starts, u32, struct offcpu_start_t, 65536);
BPF_TABLE("lru_hash", struct file_id_t, u32, tracked_files, 131072);
BPF_PERCPU_ARRAY(file_event_scratch, struct file_event_t, 1);
/* 0=full telemetry; 1=dedicated page-hotset sampling.  This changes only
 * producer selection, not any emitted event ABI. */
BPF_ARRAY(page_hotset_only, u32, 1);
BPF_PERF_OUTPUT(events);
BPF_PERF_OUTPUT(cache_events);
BPF_PERF_OUTPUT(workload_events);

static __always_inline int is_page_hotset_only(void)
{
    u32 key = 0;
    u32 *enabled = page_hotset_only.lookup(&key);
    return enabled != 0 && *enabled != 0;
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
    if (is_page_hotset_only())
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
    cache_events.perf_submit(ctx, &event, sizeof(event));
    return 0;
}

/*
 * mm_filemap_delete_from_page_cache 可能在 kswapd/writeback 等内核线程中执行，
 * 所以 evictFile 不能用“当前 PID”冒充归属。它按 device+inode 查询最近访问该
 * 文件的已定义 App tag，并把回收线程的真实 tid/comm 作为执行上下文一并上报。
 */
static __always_inline int evictFile(
    void *ctx, u64 device, u64 inode, u64 page_index, u32 page_order)
{
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
    if (is_page_hotset_only())
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

    if (item->op == OP_READ || item->op == OP_PREAD)
        readFile(ctx, event);
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
    return begin_event(OP_MMAP, (s32)args->fd, -1, -1, args->len,
                       (s64)args->off, 1, args->flags, 0, 0, 0);
}
TRACEPOINT_PROBE(syscalls, sys_exit_mmap) { return finish_event(args, args->ret); }

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
    return accessFile(args, args->s_dev, args->i_ino, args->index, args->last_index);
}

/* 文件页真正从 page cache 删除；不是“内存下降”的周期推断。 */
TRACEPOINT_PROBE(filemap, mm_filemap_delete_from_page_cache)
{
    return evictFile(args, args->s_dev, args->i_ino, args->index, args->order);
}

static __always_inline int submit_workload(
    void *ctx, u32 kind, u32 app_tag, u32 tgid, u32 tid,
    u64 value1, u64 value2, u64 value3, u64 device,
    const char *rwbs)
{
    if (is_page_hotset_only())
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
    if (is_page_hotset_only())
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
