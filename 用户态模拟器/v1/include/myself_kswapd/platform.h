#ifndef MYSELF_KSWAPD_PLATFORM_H
#define MYSELF_KSWAPD_PLATFORM_H

#include <stddef.h>
#include <stdint.h>

struct reclaim_allocator_ops {
    void *(*alloc)(void *context, size_t size);
    void *(*calloc)(void *context, size_t count, size_t size);
    void (*dealloc)(void *context, void *pointer);
};

struct reclaim_clock_ops {
    uint64_t (*get_time_ns)(void *context);
};

struct reclaim_log_ops {
    void (*log)(void *context, int level, const char *message);
};

struct reclaim_lock_ops {
    int (*init)(void *context, void **lock);
    void (*destroy)(void *context, void *lock);
    void (*lock)(void *context, void *lock);
    void (*unlock)(void *context, void *lock);
};

struct reclaim_platform {
    const struct reclaim_allocator_ops *allocator;
    void *allocator_context;
    const struct reclaim_clock_ops *clock;
    void *clock_context;
    const struct reclaim_log_ops *log;
    void *log_context;
    const struct reclaim_lock_ops *lock;
    void *lock_context;
};

struct reclaim_userspace_platform {
    struct reclaim_platform platform;
    size_t live_allocations;
    long fail_after;
    long allocation_attempts;
    uint64_t logical_time_ns;
};

void reclaim_platform_userspace_init(struct reclaim_userspace_platform *userspace);
void reclaim_platform_userspace_set_fail_after(struct reclaim_userspace_platform *userspace,
                                               long allocation_number);
size_t reclaim_platform_userspace_live_allocations(
    const struct reclaim_userspace_platform *userspace);

#endif
