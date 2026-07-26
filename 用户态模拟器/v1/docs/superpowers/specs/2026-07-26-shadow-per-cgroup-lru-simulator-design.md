# Shadow per-cgroup LRU 模拟器设计规格

**项目：** `myself-kswapd`
**日期：** 2026-07-26
**状态：** 已批准设计，尚未开始实现
**主要语言：** C11

## 1. 目的

本项目构建一个可移植、确定性、单线程的 C 模拟器，用于验证预测驱动的内存回收引擎。第一阶段重点验证：当内核只提供全局 LRU 时，如何维护按 cgroup 组织的 **Shadow per-cgroup LRU**。

模拟器不回收真实物理内存。它只模拟页面/folio 元数据、cgroup 所有权、active/inactive LRU 状态、老化、回收压力、候选隔离、执行结果、putback、统计和一致性检查。

长期集成路径如下：

1. 在用户态验证可移植核心。
2. 将同一套决策逻辑集成到 Linux，作为真实内核验证平台。
3. 将 platform 和 executor 层适配到 OpenHarmony。
4. 增加基于预测的 cgroup 保护和回收加权。

## 2. 核心架构原则

系统明确区分三个角色：

- **内核全局 LRU：** 真实 folio 状态的权威来源，也是内核回收路径实际使用的执行结构。
- **Shadow per-cgroup LRU：** 按 cgroup 建立的辅助策略视图，用于分类、保护、排序和推荐回收候选。
- **内核回收执行器：** 使用现有内核机制执行隔离、解除映射、writeback、swap、映射删除、计费更新和真实内存释放。

Shadow LRU 不拥有真实 folio 生命周期，也不能被当作权威状态。在第一阶段之后的保守内核集成中，内核仍按原有全局 LRU 顺序扫描，并查询 Shadow 元数据来允许、延迟、保护或降低当前 folio 的优先级。

## 3. 范围

### 3.1 第一阶段实现内容

- C11 可移植核心。
- CMake 和 CTest。
- 单线程、确定性的事件执行。
- 动态页面和 cgroup 元数据。
- 每个 engine 实例独立的 platform 操作表。
- 页面和 cgroup 哈希索引。
- 每个 cgroup 四条 Shadow LRU 链：inactive anon、active anon、inactive file、active file。
- 简化的 G1 老化策略。
- `AGE_GROUP` 和 `AGE_ALL`。
- `RECLAIM_GROUP` 和 `RECLAIM_ALL`。
- Linux 风格的 priority 递增压力基线。
- 可配置的 `swappiness` 和 `swap_enabled`。
- folio `order`、基础页统计和 overshoot 统计。
- 两阶段隔离、执行、putback 流程。
- 默认 SUCCESS、支持单次 outcome 注入的用户态模拟 executor。
- 测试模式下的逐事件验证。
- 文本 trace 回放和直接 C 测试 API。
- 单元、集成、场景、失败注入和确定性测试。

### 3.2 明确排除内容

第一阶段不实现：

- 真实物理内存回收。
- Linux 内核模块或内核补丁。
- OpenHarmony 内核修改。
- 真实 swap、writeback、反向映射、页表更新或 unmap。
- 多线程或并发访问。
- 完整 Linux active/inactive 老化行为。
- MGLRU。
- LSTM、Markov 或预测逻辑。
- `memory.min` 或 `memory.low` 传播。
- NUMA node 或 zone。
- Shadow 到内核的生命周期同步 hook。

## 4. 仓库布局

```text
myself-kswapd/
├── CMakeLists.txt
├── README.md
├── LICENSE
├── include/
│   └── myself_kswapd/
│       ├── engine.h
│       ├── event.h
│       ├── types.h
│       ├── error.h
│       ├── platform.h
│       ├── policy.h
│       ├── executor.h
│       ├── stats.h
│       └── validator.h
├── src/
│   ├── core/
│   │   ├── engine.c
│   │   ├── page.c
│   │   ├── domain.c
│   │   ├── hash.c
│   │   ├── list.c
│   │   ├── lru.c
│   │   ├── aging_g1.c
│   │   ├── scan_pressure.c
│   │   ├── reclaim.c
│   │   ├── stats.c
│   │   └── validator.c
│   └── simulator/
│       ├── main.c
│       ├── event_parser.c
│       ├── event_runner.c
│       ├── userspace_platform.c
│       └── simulator_executor.c
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── scenarios/
│   └── test_support/
├── adapters/
│   ├── linux/
│   │   └── README.md
│   └── openharmony/
│       └── README.md
└── docs/
    ├── architecture.md
    ├── event-format.md
    ├── porting.md
    └── superpowers/
        ├── specs/
        └── plans/
```

## 5. 构建与执行模型

### 5.1 构建产物

CMake 生成：

- `reclaim_core`：平台无关静态库。
- `reclaim_userspace`：用户态 platform、executor 和事件回放支持库。
- `reclaim_simulator`：文本 trace 回放程序。
- `reclaim_tests`：由 CTest 注册的测试程序。

项目不依赖第三方运行时库。

### 5.2 并发模型

v1 是单线程、确定性的。所有事件顺序执行。核心不包含可变全局状态，每个公开操作都接收 `reclaim_engine *`。

锁操作已抽象，但用户态模拟器使用空实现。后续内核或并发版本可以将同一接口映射到 spinlock、mutex 或其他同步机制。

## 6. 平台抽象

可移植核心不直接调用 `malloc`、`free`、`printf`、平台时钟、pthread 原语或内核函数。平台依赖通过每个 engine 实例注入的操作表提供。

代表性接口：

```c
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
```

用户态适配器将这些操作映射到 C 运行时、确定性逻辑时间、日志包装和空锁。Linux/OpenHarmony 适配器将在后续提供各自实现。操作表存储在 engine 实例中，不使用可变全局注册表。

## 7. 主要数据结构

### 7.1 Engine

```c
struct reclaim_engine {
    struct reclaim_platform platform;
    struct reclaim_engine_config config;

    struct reclaim_page_table page_table;
    struct reclaim_domain_table domain_table;

    const struct reclaim_aging_ops *aging_ops;
    const struct reclaim_domain_selector_ops *selector_ops;
    const struct reclaim_executor_ops *executor_ops;

    struct reclaim_engine_stats stats;
    uint64_t event_seq;
};
```

Engine 负责协调模块，但不承载全部策略逻辑。

### 7.2 Domain

每个 cgroup 对应一个 domain：

```c
struct reclaim_domain {
    uint64_t cgroup_id;

    struct reclaim_lru_list inactive_anon;
    struct reclaim_lru_list active_anon;
    struct reclaim_lru_list inactive_file;
    struct reclaim_lru_list active_file;

    struct reclaim_domain_config config;
    struct reclaim_domain_stats stats;

    struct reclaim_domain *hash_next;
};
```

每个 domain 恰好包含四条普通回收链。不可回收 folio 通过生命周期状态和统计跟踪，但不能放回四条普通链。domain table 同时维护哈希索引和按 `cgroup_id` 排序的辅助 intrusive 链；全局老化和回收只遍历排序链，不遍历哈希桶顺序。

### 7.3 Folio 元数据

一个 `reclaim_page` 对象表示一个不可拆分的 folio：

```c
struct reclaim_page {
    uint64_t page_id;

    uint64_t charge_cgroup_id;
    uint64_t last_access_cgroup_id;

    enum reclaim_page_type type;
    enum reclaim_page_state state;
    enum reclaim_lru_kind lru_kind;

    uint32_t order;
    uint32_t flags;

    uint64_t last_access_seq;
    uint64_t last_age_seq;
    uint32_t access_count;

    bool referenced;
    bool shared;

    enum reclaim_sim_outcome next_sim_outcome;

    struct reclaim_list_node lru_node;
    struct reclaim_page *hash_next;
};
```

一个 folio 表示的 4 KiB 基础页数为：

```c
1ULL << order
```

第一阶段绝不拆分 folio。

## 8. 所有权语义

每个 folio 只有一个内存计费 owner：

```text
charge_cgroup_id
```

创建或 charge folio 的 cgroup 拥有其 Shadow LRU。其他 cgroup 访问该 folio 时，只更新：

- `last_access_cgroup_id`；
- `shared`；
- 访问统计。

访问不会改变 `charge_cgroup_id`，也不会将 folio 移到另一个 domain。只有显式 recharge 操作可以迁移 owner。

## 9. 页面生命周期

```c
enum reclaim_page_state {
    RECLAIM_PAGE_NEW,
    RECLAIM_PAGE_ON_LRU,
    RECLAIM_PAGE_ISOLATED,
    RECLAIM_PAGE_UNEVICTABLE,
};
```

合法转换：

```text
PAGE_ADD:
NEW -> ON_LRU

候选隔离：
ON_LRU -> ISOLATED

putback：
ISOLATED -> ON_LRU

执行后重新激活：
ISOLATED -> ON_LRU，并进入 active 链

不可回收结果：
ISOLATED -> UNEVICTABLE

成功回收：
ISOLATED -> 从索引删除并释放元数据
```

成功回收的对象不会保留长期 `RECLAIMED` 状态。

## 10. 老化策略

v1 实现确定性的 `G1` 模拟策略，不宣称完整复现 Linux 页面老化。

### 10.1 访问

`PAGE_ACCESS` 执行：

```text
referenced = true
last_access_seq = current event sequence
last_access_cgroup_id = accessing cgroup
access_count += 1
```

当访问者不同于 charge owner 时设置 `shared=true`。访问事件本身不立即修改 LRU 链。

### 10.2 老化

支持：

- `AGE_GROUP <cgroup_id>`；
- `AGE_ALL`。

`AGE_ALL` 按升序 `cgroup_id` 处理 domain。

G1 转换如下：

| 当前链 | `referenced` | 结果 |
|---|---:|---|
| inactive | true | 移到 active 尾部 |
| inactive | false | 保持原位置 |
| active | true | 刷新到 active 尾部 |
| active | false | 移到 inactive 尾部 |

处理结束后清除 `referenced`，并更新 `last_age_seq`。

老化逻辑通过 `reclaim_aging_ops` 访问，以便后续替换为更接近 Linux 的策略或 MGLRU，而不改变 engine、索引、domain、executor 和 parser 层。

## 11. 回收请求

支持：

```text
RECLAIM_GROUP <cgroup_id> <target_pages>
RECLAIM_ALL <target_pages>
```

`target_pages` 始终表示 4 KiB 基础页。

### 11.1 定向回收

`RECLAIM_GROUP` 只能扫描指定 domain，不能修改其他 domain 的页面或统计。

### 11.2 全局回收

`RECLAIM_ALL` 使用简化的 Linux 风格动态压力，而不是预先分配的静态 cgroup 配额表。

每个 priority 执行：

1. 按升序 `cgroup_id` 遍历 domain。
2. 现场计算有效可回收 LRU 大小。
3. 根据以下公式计算扫描压力：

   ```c
   scan_pages = effective_lru_pages >> priority;
   ```

4. 若存在可扫描页但结果为 0，则至少尝试 1 个基础页。
5. 将扫描预算拆分到 anonymous 和 file folio。
6. 按有限批次扫描。
7. 累计 scanned、isolated、reclaimed、activated 和 putback 统计。
8. 达到目标后立即停止。

默认压力配置：

```c
struct reclaim_pressure_config {
    uint32_t default_priority;    /* 12 */
    uint32_t minimum_priority;    /* 0 */
    uint32_t scan_batch_pages;    /* 32 */
    uint32_t max_reclaim_rounds;  /* default_priority - minimum_priority + 1 */
};
```

测试可以使用更小的配置。

## 12. Anonymous/File 扫描拆分

v1 使用简化的 swappiness 模型。

默认配置为 `default_swappiness = 60`、`default_swap_enabled = true`，domain 可以覆盖这两个值。

swap 关闭时：

```text
anonymous scan budget = 0
file scan budget = total scan budget
```

swap 开启时：

```c
anon_weight = swappiness;
file_weight = 200 - swappiness;
```

某一类候选不足时，未使用预算转交另一类。第一阶段不模拟动态回收成本、refault 反馈、dirty/writeback 比例或 swap I/O 压力。

## 13. 候选隔离与 folio overshoot

回收分为两阶段：

1. 从 Shadow inactive 链选择并隔离候选。
2. 将 isolated batch 交给 executor。

隔离转换：

```text
ON_LRU -> ISOLATED
```

候选保存来源 LRU，以便批次构建失败或执行失败时安全放回。

folio 不可拆分。如果下一个 folio 的基础页数超过剩余目标，则选择整个 folio，并记录：

```text
nr_overshoot_pages = nr_reclaimed_pages - target_pages
```

## 14. Executor 模型

```c
struct reclaim_executor_ops {
    int (*execute_batch)(
        void *context,
        struct reclaim_candidate_batch *batch,
        struct reclaim_exec_result *result);
};
```

### 14.1 用户态模拟 executor

模拟器不伪造 Linux swap、writeback、unmap 或真实内存释放。每个 folio 默认得到一次 `SUCCESS`。测试可注入：

- `SUCCESS`；
- `PUTBACK`；
- `ACTIVATE`；
- `BUSY`；
- `DIRTY`；
- `WRITEBACK`；
- `UNEVICTABLE`。

注入结果只消费一次，之后恢复为 `SUCCESS`。

| 结果 | 状态更新 |
|---|---|
| `SUCCESS` | 从哈希表删除并释放元数据 |
| `PUTBACK` | 放回原 inactive 链尾 |
| `BUSY` | 放回 inactive 链尾并增加 busy 统计 |
| `DIRTY` | 放回 inactive 链尾并增加 dirty 统计 |
| `WRITEBACK` | 放回 inactive 链尾并增加 writeback 统计 |
| `ACTIVATE` | 放入对应 active 链尾 |
| `UNEVICTABLE` | 移出普通四链并设为不可回收 |

### 14.2 内核 executor

后续 Linux/OpenHarmony 适配器将替换模拟 executor，把策略候选连接到现有内核隔离和回收机制。它不能重新实现反向映射、swap、writeback、映射删除、计费或真实物理页释放。

## 15. 结果与停止语义

程序错误和回收结果必须分离。

代表性结果结构：

```c
struct reclaim_result {
    enum reclaim_stop_reason stop_reason;

    uint64_t target_pages;
    uint64_t nr_folios_scanned;
    uint64_t nr_pages_scanned;
    uint64_t nr_folios_isolated;
    uint64_t nr_pages_isolated;
    uint64_t nr_folios_reclaimed;
    uint64_t nr_pages_reclaimed;
    uint64_t nr_pages_putback;
    uint64_t nr_pages_activated;
    uint64_t nr_overshoot_pages;

    uint32_t final_priority;
};
```

停止原因包括：

- target reached；
- no scannable pages；
- no progress；
- priority exhausted；
- executor error；
- round limit reached。

请求少于目标页数并不一定是错误；只要 engine 正常运行，就可以返回正常结果。

每个 priority 完成一次 domain 遍历。当达到目标、没有候选、没有进展、耗尽 priority 或达到配置的轮数上限时停止。

## 16. 错误处理与恢复

错误码至少包括：

```c
RECLAIM_OK
RECLAIM_ERR_INVALID_ARGUMENT
RECLAIM_ERR_NO_MEMORY
RECLAIM_ERR_DOMAIN_NOT_FOUND
RECLAIM_ERR_DOMAIN_ALREADY_EXISTS
RECLAIM_ERR_DOMAIN_NOT_EMPTY
RECLAIM_ERR_PAGE_NOT_FOUND
RECLAIM_ERR_PAGE_ALREADY_EXISTS
RECLAIM_ERR_PAGE_STATE
RECLAIM_ERR_PAGE_TYPE
RECLAIM_ERR_PARSE
RECLAIM_ERR_EXECUTOR
RECLAIM_ERR_VALIDATION
RECLAIM_ERR_NOT_SUPPORTED
RECLAIM_ERR_INTERNAL
```

要求：

- 输入必须先验证再修改状态。
- 分配资源必须先完成，提交结构变化前不得留下半成品。
- 事件失败后保持之前状态不变。
- 部分构建的 candidate batch 必须回滚。
- executor 失败时所有未完成 isolated folio 必须 putback。
- 部分回收是正常结果，不是程序错误。
- validator 失败表示内部 bug，必须终止并报告。
- 销毁非空 domain 返回 `DOMAIN_NOT_EMPTY`。
- 销毁 engine 时释放所有剩余对象。

内部回滚的 putback 将页面放回正确类别的链尾；v1 不要求恢复到精确的原链位置。

## 17. 一致性验证

```c
int reclaim_engine_validate(
    const struct reclaim_engine *engine,
    struct reclaim_validation_report *report);
```

必须检查：

1. folio 至多挂在一条 LRU 链上。
2. 每个 `ON_LRU` folio 属于有效 domain。
3. LRU 所属 domain 等于 `charge_cgroup_id`。
4. `ISOLATED` folio 不在任何普通 LRU 上。
5. `UNEVICTABLE` folio 不在任何普通 LRU 上。
6. anonymous/file 类型与 LRU 类型匹配。
7. active/inactive 元数据与链类型匹配。
8. 哈希索引、LRU 和生命周期状态一致。
9. 每条 LRU 的 folio 数和基础页数与遍历结果一致。
10. domain 汇总等于 engine 全局统计。
11. 已删除页面不在任何索引中。
12. 不存在重复链表节点。

验证报告包含 event sequence、page ID、cgroup ID、违反的规则、期望值和观测值。

## 18. 事件层

直接 C 测试和文本 trace 必须调用同一套公开核心 API。

初始事件包括：

```text
GROUP_CREATE <cgroup_id>
GROUP_DESTROY <cgroup_id>
GROUP_SET_SWAPPINESS <cgroup_id> <INHERIT|0..200>
GROUP_SET_SWAP_ENABLED <cgroup_id> <INHERIT|0|1>
GROUP_SET_SWAP <cgroup_id> <INHERIT|ON|OFF>
PAGE_ADD <page_id> <cgroup_id> <ANON|FILE> <order>
PAGE_REMOVE <page_id>
PAGE_ACCESS <page_id> <access_cgroup_id>
PAGE_RECHARGE <page_id> <new_cgroup_id>
PAGE_MIGRATE <old_page_id> <new_page_id>
AGE_GROUP <cgroup_id>
AGE_ALL
RECLAIM_GROUP <cgroup_id> <target_pages>
RECLAIM_ALL <target_pages>
PAGE_EXEC_OUTCOME <page_id> <outcome>
VALIDATE
DUMP
```

`PAGE_RECHARGE` 只允许作用于非 isolated folio，并保留页面类型、老化元数据和 active/inactive 类别，同时迁移 owner 与链归属。`PAGE_MIGRATE` 更改模拟页面 ID，但保留 owner、生命周期、类型和 LRU 类别；目标 ID 不得已存在。`PAGE_REMOVE` 拒绝 isolated folio。

场景测试可以增加断言事件，例如页面缺失、生命周期状态、LRU、owner、继承配置和计数器。

解析错误必须报告文件名、行号、原始文本和原因；非法事件不执行，默认停止回放，最后仍执行结束验证。

## 19. 确定性

v1 输出不得依赖墙钟时间、线程调度、指针地址、随机哈希种子或哈希桶遍历顺序。

确定性规则：

- `event_seq` 作为逻辑时钟。
- domain 通过按 `cgroup_id` 排序的链按升序处理。
- 稳定 DUMP 按 `page_id` 排序，不能输出哈希桶顺序或指针地址。
- LRU 头尾语义固定。
- 预算余数按固定顺序分配。
- trace 输出排除所有不稳定地址信息。

同一 trace 和配置在同一支持平台上重复运行时，输出必须逐字节一致。

## 20. 测试

CTest 测试覆盖：

- intrusive list；
- page/domain 哈希索引；
- allocator ops；
- 分配失败注入；
- LRU 插入、删除和移动；
- folio order 到基础页换算；
- swappiness 预算；
- priority 扫描量；
- 统计累计和 overshoot；
- 基础生命周期；
- 四链转换；
- 跨 cgroup 访问不迁移 owner；
- `AGE_GROUP` 隔离性；
- `AGE_ALL` 稳定顺序；
- `RECLAIM_GROUP` 隔离性；
- `RECLAIM_ALL` priority 递减；
- swap disabled 不扫描 anon；
- swappiness 0/60/200；
- 大 folio 不拆分并记录 overshoot；
- 所有模拟执行结果；
- 所有页面 BUSY 时的 no-progress 终止；
- executor 异常安全回滚；
- 非法事件无部分修改；
- 重复 trace 输出一致；
- engine 销毁后分配计数归零；
- validator 对故意破坏状态的检测。

Debug 配置至少启用：

```text
-Wall
-Wextra
-Wpedantic
-Werror
AddressSanitizer
UndefinedBehaviorSanitizer
```

ThreadSanitizer 延后到并发阶段。

验收标准：

- 所有 CTest 通过；
- 编译零 warning；
- 无 ASan 泄漏、越界或 use-after-free；
- 无 UBSan 报告；
- 每个成功事件后的验证通过；
- 重复 trace 输出一致；
- 错误路径无 isolated folio 残留；
- engine 销毁后 tracked allocation 为零。

## 21. 内核集成方向

### 21.1 Linux 验证阶段

第一阶段 Linux 集成必须保持保守：

```text
内核全局 LRU 扫描顺序
        -> 查询 Shadow 策略元数据
        -> 允许、延迟、保护或降低当前 folio 优先级
        -> 使用现有内核隔离与回收
        -> 回报真实扫描/回收反馈
        -> 同步或修复 Shadow 元数据
```

Shadow 层不得直接操作原始内核 LRU 节点，也不得替换 vmscan。

### 21.2 OpenHarmony 阶段

OpenHarmony 适配器必须先确认：

- 实际内核版本；
- page 或 folio 结构；
- 全局 LRU 实现；
- cgroup v1 memory accounting 行为；
- 现有回收调用图；
- 可访问的生命周期 hook 点；
- 安全的隔离和 putback 接口。

只有事实确认后，才能映射 platform 操作、页面身份、同步 hook 和 executor 反馈。

## 22. 预测扩展方向

预测不属于 v1。后续预测输入可以包括：

- 下一个应用概率；
- 前台/后台状态；
- CONTINUE 和 REENTRY 提示；
- workload 状态；
- TTL；
- confidence；
- Markov transition。

预测只能修改策略量，例如：

- cgroup 保护权重；
- cgroup 回收权重；
- 扫描预算；
- 候选过滤；
- 候选优先级。

预测不能替代内核页面状态验证或底层回收机制。

## 23. 设计总结

第一阶段是一个平台无关、确定性的模拟器，用于证明 Shadow per-cgroup LRU 策略引擎的数据结构和状态转换正确。它提供 cgroup 组织、老化、压力递增、anonymous/file 选择、folio 统计、两阶段回收、回滚、显式测试结果和严格验证。

未来内核集成中，真实全局 LRU 仍是权威状态和执行结构。Shadow 元数据只提供策略建议，真实回收成功与否由现有内核内存管理路径决定。
