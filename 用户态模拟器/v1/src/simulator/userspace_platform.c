#include "myself_kswapd/platform.h"

#include <stdio.h>
#include <stdlib.h>

struct userspace_allocation_header {
    struct reclaim_userspace_platform *owner;
};

static void *userspace_alloc(void *context, size_t size)
{
    struct reclaim_userspace_platform *platform = context;
    struct userspace_allocation_header *header;

    if (platform->fail_after >= 0L && platform->allocation_attempts >= platform->fail_after) {
        platform->allocation_attempts++;
        return NULL;
    }
    platform->allocation_attempts++;
    header = malloc(sizeof(*header) + size);
    if (header == NULL) {
        return NULL;
    }
    header->owner = platform;
    platform->live_allocations++;
    return header + 1;
}

static void *userspace_calloc(void *context, size_t count, size_t size)
{
    struct reclaim_userspace_platform *platform = context;
    struct userspace_allocation_header *header;
    size_t total;

    if (size != 0U && count > SIZE_MAX / size) {
        return NULL;
    }
    total = count * size;
    if (platform->fail_after >= 0L && platform->allocation_attempts >= platform->fail_after) {
        platform->allocation_attempts++;
        return NULL;
    }
    platform->allocation_attempts++;
    header = calloc(1U, sizeof(*header) + total);
    if (header == NULL) {
        return NULL;
    }
    header->owner = platform;
    platform->live_allocations++;
    return header + 1;
}

static void userspace_dealloc(void *context, void *pointer)
{
    struct userspace_allocation_header *header;
    struct reclaim_userspace_platform *platform;

    (void)context;
    if (pointer == NULL) {
        return;
    }
    header = ((struct userspace_allocation_header *)pointer) - 1;
    platform = header->owner;
    if (platform->live_allocations > 0U) {
        platform->live_allocations--;
    }
    free(header);
}

static uint64_t userspace_time(void *context)
{
    struct reclaim_userspace_platform *platform = context;
    return ++platform->logical_time_ns;
}

static void userspace_log(void *context, int level, const char *message)
{
    (void)context;
    (void)level;
    (void)message;
}

static int userspace_lock_init(void *context, void **lock)
{
    (void)context;
    if (lock == NULL) {
        return -1;
    }
    *lock = NULL;
    return 0;
}

static void userspace_lock_destroy(void *context, void *lock)
{
    (void)context;
    (void)lock;
}

static void userspace_lock(void *context, void *lock)
{
    (void)context;
    (void)lock;
}

static void userspace_unlock(void *context, void *lock)
{
    (void)context;
    (void)lock;
}

static const struct reclaim_allocator_ops allocator_ops = {
    .alloc = userspace_alloc,
    .calloc = userspace_calloc,
    .dealloc = userspace_dealloc,
};

static const struct reclaim_clock_ops clock_ops = {.get_time_ns = userspace_time};
static const struct reclaim_log_ops log_ops = {.log = userspace_log};
static const struct reclaim_lock_ops lock_ops = {
    .init = userspace_lock_init,
    .destroy = userspace_lock_destroy,
    .lock = userspace_lock,
    .unlock = userspace_unlock,
};

void reclaim_platform_userspace_init(struct reclaim_userspace_platform *userspace)
{
    if (userspace == NULL) {
        return;
    }
    *userspace = (struct reclaim_userspace_platform){
        .platform = {
            .allocator = &allocator_ops,
            .allocator_context = userspace,
            .clock = &clock_ops,
            .clock_context = userspace,
            .log = &log_ops,
            .log_context = userspace,
            .lock = &lock_ops,
            .lock_context = userspace,
        },
        .fail_after = -1L,
    };
}

void reclaim_platform_userspace_set_fail_after(struct reclaim_userspace_platform *userspace,
                                               long allocation_number)
{
    if (userspace != NULL) {
        userspace->fail_after = allocation_number;
        userspace->allocation_attempts = 0L;
    }
}

size_t reclaim_platform_userspace_live_allocations(
    const struct reclaim_userspace_platform *userspace)
{
    return userspace == NULL ? 0U : userspace->live_allocations;
}
