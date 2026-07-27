/**
 * @file reclaim.c
 * @brief 页面回收核心逻辑 —— 模拟 Linux kswapd 的 LRU 扫描、隔离、执行回收流程
 *
 * 整体流程（对标内核 shrink_node / shrink_lruvec）：
 *   1. select_domain  → 按 swappiness 拆分匿名页/文件页扫描预算
 *   2. select_list    → 从 inactive LRU 链表摘取候选页（隔离）
 *   3. execute_batch  → 调用执行器模拟尝试回收（解除映射、写入交换、释放）
 *   4. 根据执行结果  → 激活 / 放回 / 标记 unevictable / 释放
 *
 * 优先级循环（reclaim_run）：
 *   从 default_priority 逐步升高（值变小 = 更激进），直到 target 达成或无页可扫。
 *   对应内核中 sc->priority 从 DEF_PRIORITY 降到 0 的过程。
 */

#include "internal.h"

#include <stdint.h>

/* ==========================================================================
 *  辅助函数
 * ========================================================================== */

/**
 * @brief 初始化回收结果结构体
 * @param result       输出结果
 * @param target_pages 本次回收的目标页数
 */
static void result_init(struct reclaim_result *result, uint64_t target_pages)
{
    *result = (struct reclaim_result){
        .error = RECLAIM_OK,
        .stop_reason = RECLAIM_STOP_PRIORITY_EXHAUSTED,  /* 默认：优先级耗尽（未达目标） */
        .target_pages = target_pages,
    };
}

/**
 * @brief 获取页面在 base page（4K）粒度下的页数
 *
 * order=0 → 1 页, order=1 → 2 页, order=9 → 512 页（2MB huge page）
 * 内部调用 reclaim_folio_base_pages，忽略返回值（该函数总是成功）。
 */
static uint64_t page_base_pages(const struct reclaim_page *page)
{
    uint64_t pages = 0U;
    (void)reclaim_folio_base_pages(page->order, &pages);
    return pages;
}

/* ==========================================================================
 *  页面状态变更 —— 对应内核 folio_activate / folio_putback / folio_unevictable
 * ========================================================================== */

/**
 * @brief 将隔离失败的页面放回对应 inactive LRU
 * @param engine     回收引擎
 * @param page       待放回的页面
 * @param source_lru 页面被隔离前所在的 LRU 类型（ANON / FILE）
 *
 * 对应内核的 putback_inactive_folios —— 扫描过程中发现页面不适合回收时，
 * 把它放回所在的 inactive 链表（不是 active，因为还没被访问证明为"热"）。
 */
static void putback_page(struct reclaim_engine *engine,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind source_lru)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    /* 根据来源 LRU 类型决定放回的 inactive 链表 */
    enum reclaim_lru_kind inactive = reclaim_lru_is_anon(source_lru) ?
        RECLAIM_LRU_INACTIVE_ANON : RECLAIM_LRU_INACTIVE_FILE;
    if (domain != NULL) {
        (void)reclaim_link_page(engine, page, domain, inactive, RECLAIM_PAGE_ON_LRU);
    }
}

/**
 * @brief 将页面激活（提升到 active LRU）
 * @param engine     回收引擎
 * @param page       待激活的页面
 * @param source_lru 页面来源 LRU 类型
 *
 * 对应内核的 folio_activate —— 在回收过程中如果检测到页面被访问（PG_referenced），
 * 说明它是"热"页，不应该回收，而是提升到 active 链表给予第二次机会。
 */
static void activate_page(struct reclaim_engine *engine,
                          struct reclaim_page *page,
                          enum reclaim_lru_kind source_lru)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    /* 根据来源 LRU 类型决定目标 active 链表 */
    enum reclaim_lru_kind active = reclaim_lru_is_anon(source_lru) ?
        RECLAIM_LRU_ACTIVE_ANON : RECLAIM_LRU_ACTIVE_FILE;
    if (domain != NULL) {
        (void)reclaim_link_page(engine, page, domain, active, RECLAIM_PAGE_ON_LRU);
    }
}

/**
 * @brief 标记页面为不可回收（unevictable）
 * @param engine 回收引擎
 * @param page   待标记的页面
 *
 * 对应内核的 folio_set_unevictable —— mlock 或 ramfs 等场景下页面不允许回收。
 * 标记后页面脱离 LRU（lru_kind = LRU_NONE），计入 unevictable 统计。
 */
static void mark_unevictable(struct reclaim_engine *engine, struct reclaim_page *page)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    uint64_t pages = page_base_pages(page);
    if (domain != NULL) {
        domain->stats.nr_unevictable_folios++;
        domain->stats.nr_unevictable_pages += pages;
    }
    engine->stats.nr_unevictable_folios++;
    engine->stats.nr_unevictable_pages += pages;
    page->state = RECLAIM_PAGE_UNEVICTABLE;
    page->lru_kind = RECLAIM_LRU_NONE;
}

/**
 * @brief 释放已成功回收的页面
 * @param engine 回收引擎
 * @param page   待释放的页面
 *
 * 从哈希表中移除 → 更新全局回收统计 → 释放内存。
 * 对应内核 __free_one_page 后的 buddy 归还流程。
 */
static void free_reclaimed_page(struct reclaim_engine *engine, struct reclaim_page *page)
{
    reclaim_page_hash_remove(engine, page);
    engine->stats.nr_reclaimed_folios++;
    engine->stats.nr_reclaimed_pages += page_base_pages(page);
    reclaim_free(engine, page);
}

/* ==========================================================================
 *  扫描选择 —— 对应内核 isolate_lru_folios
 * ========================================================================== */

/**
 * @brief 将一个页面加入回收候选批次
 * @param engine     回收引擎
 * @param batch      候选批次（输出，追加到末尾）
 * @param page       待加入的页面
 * @param source_lru 页面来源 LRU
 * @param result     回收结果统计（更新扫描/隔离计数）
 * @return RECLAIM_OK 成功，RECLAIM_ERR_NO_MEMORY 批次已满
 *
 * 对应内核的 __isolate_lru_folio —— 从 LRU 链表摘除页面，放入隔离列表。
 * - 检查批次容量（避免溢出）
 * - 从 domain LRU 摘除 → 标记 ISOLATED 状态
 * - 更新扫描/隔离统计计数器
 */
static int add_candidate(struct reclaim_engine *engine,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind source_lru,
                         struct reclaim_result *result)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    uint64_t pages = page_base_pages(page);

    /* 批次已满或 domain 无效则拒绝 */
    if (batch->count == batch->capacity || domain == NULL) return RECLAIM_ERR_NO_MEMORY;
    /* 添加到批次的候选数组 */
    batch->items[batch->count++] = (struct reclaim_candidate){
        .page = page,
        .source_lru = source_lru,
        .outcome = RECLAIM_SIM_SUCCESS,  /* 初始假设：回收成功 */
    };
    /* 从 domain 的 LRU 链表上摘除该页面 */
    reclaim_unlink_page(engine, page, domain);
    page->state = RECLAIM_PAGE_ISOLATED;  /* 标记为已隔离——类似 PG_isolated */
    /* 更新扫描隔离统计 */
    result->nr_folios_scanned++;
    result->nr_pages_scanned += pages;
    result->nr_folios_isolated++;
    result->nr_pages_isolated += pages;
    engine->stats.nr_scanned_folios++;
    engine->stats.nr_scanned_pages += pages;
    engine->stats.nr_isolated_folios++;
    engine->stats.nr_isolated_pages += pages;
    return RECLAIM_OK;
}

/**
 * @brief 从单个 LRU 链表中选择候选页面
 * @param engine 回收引擎
 * @param list   目标 LRU 链表
 * @param kind   LRU 类型
 * @param budget 本次最多选择的页数（预算）
 * @param batch  候选批次（输出）
 * @param result 回收结果统计
 * @return RECLAIM_OK 或 RECLAIM_ERR_NO_MEMORY
 *
 * 遍历 inactive LRU 的 head→tail 方向（FIFO，对应内核 LRU tail 扫描），
 * 逐个摘取页面直到预算耗尽或链表为空。
 */
static int select_list(struct reclaim_engine *engine,
                       struct reclaim_list *list,
                       enum reclaim_lru_kind kind,
                       uint64_t budget,
                       struct reclaim_candidate_batch *batch,
                       struct reclaim_result *result)
{
    struct reclaim_list_node *node = list->head.next;
    uint64_t selected_pages = 0U;
    /* 从链表头（最老 = 最先被扫描）向尾遍历 */
    while (node != &list->head && selected_pages < budget) {
        struct reclaim_list_node *next = node->next;  /* 提前保存后继：add_candidate 可能修改 node */
        struct reclaim_page *page = node->owner;
        uint64_t pages = page_base_pages(page);
        int error = add_candidate(engine, batch, page, kind, result);
        if (error != RECLAIM_OK) return error;  /* 批次满则提前返回 */
        selected_pages += pages;
        node = next;
    }
    return RECLAIM_OK;
}

/* ==========================================================================
 *  Domain 级扫描 —— 对应内核 shrink_lruvec
 * ========================================================================== */

/**
 * @brief 从单个 domain（cgroup）中选择候选页面
 * @param engine          回收引擎
 * @param domain          目标内存域
 * @param target_remaining 距离目标还差多少页
 * @param priority        当前回收优先级
 * @param batch           候选批次（输出）
 * @param result          回收结果统计
 * @return RECLAIM_OK 或错误码
 *
 * 对应内核 get_scan_count → shrink_list 的流程：
 *   1. 根据 swappiness 和 swap_enabled 拆分匿名/文件扫描配额
 *   2. 施加 scan_batch_pages 上限（避免单次扫描太多）
 *   3. 先扫 inactive_anon，再扫 inactive_file
 *
 * 注意：这个模拟器只扫描 inactive 链表。
 * 内核在低优先级时还会 deactivate active 链表，这里做了简化。
 */
static int select_domain(struct reclaim_engine *engine,
                         struct reclaim_domain *domain,
                         uint64_t target_remaining,
                         uint32_t priority,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_result *result)
{
    uint64_t anon_available = domain->inactive_anon.nr_base_pages;
    uint64_t file_available = domain->inactive_file.nr_base_pages;
    uint64_t effective = anon_available + file_available;
    uint64_t scan_budget = reclaim_scan_pages(effective, priority);
    uint64_t anon_budget;
    uint64_t file_budget;
    uint64_t total_available;

    /* 无可扫描页面或目标已达成 → 直接返回 */
    if (effective == 0U || target_remaining == 0U) return RECLAIM_OK;

    /* 扫描预算不超过剩余目标（避免过度扫描） */
    if (scan_budget > target_remaining) scan_budget = target_remaining;

    /* 施加单批次扫描上限（防止单轮扫描过多页面导致延迟尖峰） */
    if (engine->config.pressure.scan_batch_pages > 0U &&
        scan_budget > engine->config.pressure.scan_batch_pages) {
        scan_budget = engine->config.pressure.scan_batch_pages;
    }

    /* 按 swappiness 比例拆分匿名页和文件页的扫描配额 */
    reclaim_split_scan_budget(scan_budget,
                              domain->config.swappiness,
                              domain->config.swap_enabled,
                              anon_available,
                              file_available,
                              &anon_budget,
                              &file_budget);

    total_available = anon_budget + file_budget;
    if (total_available == 0U) return RECLAIM_OK;

    /* 先扫描匿名页 inactive 链表 */
    if (select_list(engine, &domain->inactive_anon, RECLAIM_LRU_INACTIVE_ANON,
                    anon_budget, batch, result) != RECLAIM_OK) return RECLAIM_ERR_NO_MEMORY;
    /* 再扫描文件页 inactive 链表 */
    if (select_list(engine, &domain->inactive_file, RECLAIM_LRU_INACTIVE_FILE,
                    file_budget, batch, result) != RECLAIM_OK) return RECLAIM_ERR_NO_MEMORY;
    return RECLAIM_OK;
}

/* ==========================================================================
 *  批量执行 —— 对应内核 shrink_folio_list
 * ========================================================================== */

/**
 * @brief 回滚批次中的隔离页面——放回对应 inactive LRU
 * @param engine 回收引擎
 * @param batch  待回滚的候选批次
 * @param result 回收结果（更新 putback 计数）
 *
 * 当 execute_batch 失败时调用，将已隔离但尚未执行的页面全部放回。
 * 只处理仍然处于 ISOLATED 状态的页面。
 */
static void rollback_batch(struct reclaim_engine *engine,
                           struct reclaim_candidate_batch *batch,
                           struct reclaim_result *result)
{
    size_t i;
    for (i = 0U; i < batch->count; i++) {
        struct reclaim_page *page = batch->items[i].page;
        if (page->state == RECLAIM_PAGE_ISOLATED) {
            uint64_t pages = page_base_pages(page);
            putback_page(engine, page, batch->items[i].source_lru);
            result->nr_pages_putback += pages;
        }
    }
}

/**
 * @brief 执行候选批次回收
 * @param engine 回收引擎
 * @param batch  候选批次
 * @param result 回收结果（输出执行后的分类统计）
 * @return RECLAIM_OK 或 RECLAIM_ERR_EXECUTOR
 *
 * 对应内核 shrink_folio_list 的逐页处理。
 * 将批次交给 executor（模拟器"真正回收"的组件），根据返回值分类：
 *   - SUCCESS     → 释放页面（free_reclaimed_page）
 *   - ACTIVATE    → 提升到 active LRU（页面正在被访问）
 *   - UNEVICTABLE → 标记不可回收（mlock 等）
 *   - PUTBACK/BUSY/DIRTY/WRITEBACK → 放回 inactive
 */
static int execute_batch(struct reclaim_engine *engine,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_result *result)
{
    struct reclaim_exec_result execution;
    size_t i;

    if (batch->count == 0U) return RECLAIM_OK;

    /* 调用执行器模拟实际回收操作 */
    if (engine->executor_ops->execute_batch(engine->executor_context, batch, &execution) != 0 ||
        execution.error != 0) {
        /* 执行器出错 → 回滚所有隔离页面 */
        rollback_batch(engine, batch, result);
        return RECLAIM_ERR_EXECUTOR;
    }

    /* 逐页处理执行结果 */
    for (i = 0U; i < batch->count; i++) {
        struct reclaim_candidate *candidate = &batch->items[i];
        struct reclaim_page *page = candidate->page;
        uint64_t pages = page_base_pages(page);

        switch (candidate->outcome) {
        case RECLAIM_SIM_SUCCESS:
            /* 回收成功 → 释放页面，计入 reclaimed 统计 */
            free_reclaimed_page(engine, page);
            result->nr_folios_reclaimed++;
            result->nr_pages_reclaimed += pages;
            break;
        case RECLAIM_SIM_ACTIVATE:
            /* 页面被访问过 → 提升到 active LRU */
            activate_page(engine, page, candidate->source_lru);
            result->nr_pages_activated += pages;
            break;
        case RECLAIM_SIM_UNEVICTABLE:
            /* 页面不可回收（mlock）→ 标记为 unevictable */
            mark_unevictable(engine, page);
            break;
        case RECLAIM_SIM_PUTBACK:
        case RECLAIM_SIM_BUSY:
        case RECLAIM_SIM_DIRTY:
        case RECLAIM_SIM_WRITEBACK:
            /* 暂时无法回收 → 放回 inactive LRU */
            putback_page(engine, page, candidate->source_lru);
            result->nr_pages_putback += pages;
            /* 细粒度的放回原因统计 */
            if (candidate->outcome == RECLAIM_SIM_BUSY) engine->stats.nr_busy++;
            if (candidate->outcome == RECLAIM_SIM_DIRTY) engine->stats.nr_dirty++;
            if (candidate->outcome == RECLAIM_SIM_WRITEBACK) engine->stats.nr_writeback++;
            break;
        default:
            /* 未知结果类型 → 内部错误 */
            return RECLAIM_ERR_INTERNAL;
        }
    }
    return RECLAIM_OK;
}

/* ==========================================================================
 *  回收主循环 —— 对应内核 shrink_node / do_try_to_free_pages
 * ========================================================================== */

/**
 * @brief 执行单轮回收扫描
 * @param engine         回收引擎
 * @param only_domain    指定 domain（非 NULL 时只扫描该 domain；NULL 则遍历所有）
 * @param target_remaining 距离目标剩余页数
 * @param priority       当前优先级
 * @param result         回收结果（输入的同时也作为输出累积统计）
 * @param had_candidates 输出：本轮是否选到了候选页
 * @param had_reclaim    输出：本轮是否有页面被成功回收
 * @return RECLAIM_OK 或错误码
 *
 * 一轮回收 = select（选候选）→ execute（执行回收）。
 * 分配临时 batch 数组（容量 64），执行完后释放 batch 内存。
 * 如果只指定了一个 cgroup（only_domain != NULL），只扫该 cgroup；
 * 否则按 sorted_head 顺序（通常在 main 中按压力排序）遍历所有 domain。
 */
static int reclaim_round(struct reclaim_engine *engine,
                         struct reclaim_domain *only_domain,
                         uint64_t target_remaining,
                         uint32_t priority,
                         struct reclaim_result *result,
                         bool *had_candidates,
                         bool *had_reclaim)
{
    struct reclaim_domain *domain;
    struct reclaim_candidate_batch batch;
    int error;

    /* 分配候选批次数组（固定容量 64，对应内核 SWAP_CLUSTER_MAX） */
    batch.capacity = 64U;
    batch.count = 0U;
    batch.items = reclaim_calloc(engine, batch.capacity, sizeof(*batch.items));
    if (batch.items == NULL) return RECLAIM_ERR_NO_MEMORY;

    if (only_domain != NULL) {
        /* 模式 1：只扫描指定的单个 cgroup */
        error = select_domain(engine, only_domain, target_remaining, priority, &batch, result);
        if (error != RECLAIM_OK) {
            rollback_batch(engine, &batch, result);
            reclaim_free(engine, batch.items);
            return error;
        }
    } else {
        /* 模式 2：按压力排序遍历所有 domain（全局回收） */
        for (domain = engine->domains.sorted_head; domain != NULL;
             domain = domain->sorted_next) {
            /* 已达目标 → 跳出循环 */
            uint64_t reclaimed = result->nr_pages_reclaimed;
            if (reclaimed >= result->target_pages) break;
            error = select_domain(engine, domain, result->target_pages - reclaimed,
                                  priority, &batch, result);
            if (error != RECLAIM_OK) {
                rollback_batch(engine, &batch, result);
                reclaim_free(engine, batch.items);
                return error;
            }
        }
    }

    /* 有候选页 → 执行批量回收 */
    if (batch.count > 0U) {
        *had_candidates = true;
        error = execute_batch(engine, &batch, result);
        if (error != RECLAIM_OK) {
            reclaim_free(engine, batch.items);
            return error;
        }
        if (result->nr_pages_reclaimed > 0U) *had_reclaim = true;
    }
    reclaim_free(engine, batch.items);
    return RECLAIM_OK;
}

/**
 * @brief 运行完整的回收流程（优先级循环）
 * @param engine      回收引擎
 * @param only_domain 指定 cgroup（NULL=全局回收）
 * @param target_pages 回收目标页数
 * @param result      回收结果（输出）
 * @return RECLAIM_OK 或错误码
 *
 * 这是回收的核心入口，对应内核 do_try_to_free_pages / try_to_free_mem_cgroup_pages：
 *
 *   for (priority = DEF_PRIORITY; priority >= 0; priority--) {
 *       shrink_node();  // → 对应 reclaim_round
 *       if (nr_reclaimed >= nr_to_reclaim) break;
 *       if (priority == 0 && nothing_reclaimed) break;
 *   }
 *
 * 优先级从 default_priority 递减到 minimum_priority（值越小越激进——扫描更多页）。
 * 每一轮调用 reclaim_round 做 select + execute。
 *
 * 停止条件（对应内核的 stop 语义）：
 *   - RECLAIM_STOP_TARGET_REACHED    → 回收量达标
 *   - RECLAIM_STOP_PRIORITY_EXHAUSTED → 优先级降到最低仍不足
 *   - RECLAIM_STOP_NO_SCANNABLE_PAGES → 无页可扫（LRU 空）
 *   - RECLAIM_STOP_NO_PROGRESS       → 能扫到页但无法回收（所有页都被访问/加锁）
 *   - RECLAIM_STOP_ROUND_LIMIT       → 轮次超限
 *   - RECLAIM_STOP_EXECUTOR_ERROR    → 执行器报错
 */
static int reclaim_run(struct reclaim_engine *engine,
                       struct reclaim_domain *only_domain,
                       uint64_t target_pages,
                       struct reclaim_result *result)
{
    uint32_t priority;
    uint32_t rounds = 0U;
    bool had_candidates = false;  /* 整个优先级循环中是否选到过候选页 */
    bool had_reclaim = false;     /* 整个优先级循环中是否有成功回收 */
    int error;

    result_init(result, target_pages);

    /* 目标为 0：空回收，直接标记完成 */
    if (target_pages == 0U) {
        result->stop_reason = RECLAIM_STOP_TARGET_REACHED;
        return RECLAIM_OK;
    }

    /* 事件序列号递增：通知外部观测者本轮回收开始 */
    engine->event_seq++;

    /* 优先级循环：从 default_priority 降到 minimum_priority */
    for (priority = engine->config.pressure.default_priority;; priority--) {
        bool round_candidates = false;
        bool round_reclaim = false;

        /* 执行单轮回收 */
        error = reclaim_round(engine, only_domain, target_pages - result->nr_pages_reclaimed,
                              priority, result, &round_candidates, &round_reclaim);
        result->final_priority = priority;

        /* 内存不足错误 → 无法继续 */
        if (error == RECLAIM_ERR_NO_MEMORY) {
            result->error = error;
            return error;
        }
        /* 其他错误（如执行器错误）→ 记录并退出 */
        if (error != RECLAIM_OK) {
            result->error = error;
            result->stop_reason = RECLAIM_STOP_EXECUTOR_ERROR;
            return error;
        }

        /* 更新全局标志 */
        had_candidates = had_candidates || round_candidates;
        had_reclaim = had_reclaim || round_reclaim;
        rounds++;

        /* 停止条件 1：回收量已达标 */
        if (result->nr_pages_reclaimed >= target_pages) {
            result->stop_reason = RECLAIM_STOP_TARGET_REACHED;
            break;
        }

        /* 停止条件 2：没有候选页可扫描（LRU 为空） */
        if (!round_candidates) {
            result->stop_reason = had_candidates ? RECLAIM_STOP_NO_SCANNABLE_PAGES :
                                                   RECLAIM_STOP_NO_SCANNABLE_PAGES;
            break;
        }

        /* 停止条件 3：有候选页但本轮没有任何页面被回收（全部激活或放回） */
        if (round_candidates && !round_reclaim) {
            result->stop_reason = RECLAIM_STOP_NO_PROGRESS;
            break;
        }

        /* 停止条件 4：轮次超限 */
        if (rounds >= engine->config.pressure.max_reclaim_rounds) {
            result->stop_reason = RECLAIM_STOP_ROUND_LIMIT;
            break;
        }

        /* 停止条件 5：优先级已降到最低 */
        if (priority == engine->config.pressure.minimum_priority) {
            result->stop_reason = had_reclaim ? RECLAIM_STOP_PRIORITY_EXHAUSTED :
                                                RECLAIM_STOP_NO_PROGRESS;
            break;
        }
    }

    /* 计算超额回收量（回收的比目标多） */
    result->nr_overshoot_pages = result->nr_pages_reclaimed > target_pages ?
        result->nr_pages_reclaimed - target_pages : 0U;
    engine->stats.nr_overshoot_pages += result->nr_overshoot_pages;

    return RECLAIM_OK;
}

/* ==========================================================================
 *  公开 API
 * ========================================================================== */

/**
 * @brief 对指定 cgroup 执行页面回收
 * @param engine       回收引擎
 * @param cgroup_id    目标 cgroup ID
 * @param target_pages 期望回收的页数
 * @param result       回收结果（输出）
 * @return RECLAIM_OK 或错误码
 *
 * 对应内核 try_to_free_mem_cgroup_pages —— 只在指定 memcg 内回收。
 */
int reclaim_engine_reclaim_group(struct reclaim_engine *engine,
                                 uint64_t cgroup_id,
                                 uint64_t target_pages,
                                 struct reclaim_result *result)
{
    struct reclaim_domain *domain;
    if (engine == NULL || result == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    return reclaim_run(engine, domain, target_pages, result);
}

/**
 * @brief 执行全局页面回收（遍历所有 cgroup）
 * @param engine       回收引擎
 * @param target_pages 期望回收的页数
 * @param result       回收结果（输出）
 * @return RECLAIM_OK 或错误码
 *
 * 对应内核 do_try_to_free_pages / kswapd balance_pgdat —— 全局内存压力下的回收。
 * 遍历所有 domain（按 sorted_head 顺序），直到目标达成或无域可扫。
 */
int reclaim_engine_reclaim_all(struct reclaim_engine *engine,
                               uint64_t target_pages,
                               struct reclaim_result *result)
{
    if (engine == NULL || result == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    return reclaim_run(engine, NULL, target_pages, result);
}
