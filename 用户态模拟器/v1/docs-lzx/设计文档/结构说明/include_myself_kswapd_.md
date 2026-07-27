这个目录定义的是：
> **外部模块如何使用 reclaim engine。**
这里尽量只放稳定接口、公共结构和公共枚举，不放具体算法实现。
可以理解为：
```
include/
= 对外说明书

src/core/
= 说明书背后的具体实现
```


#### **engine.h**
整个回收引擎的**总入口接口**。
它通常声明：
```
struct reclaim_engine;
struct reclaim_engine_config;
```
以及创建、销毁和核心操作：
```
int reclaim_engine_create(...);
void reclaim_engine_destroy(...);

int reclaim_engine_process_event(...);
int reclaim_engine_reclaim_group(...);
int reclaim_engine_reclaim_all(...);
```
它的作用相当于项目的“总控制器接口”。
外部调用者通常只需要拿到：
```
struct reclaim_engine *engine;
```
然后通过公开接口操作，不应该直接修改内部哈希表或 LRU。

#### **event.h**
定义统一事件模型。
例如：
```
enum reclaim_event_type {
    RECLAIM_EVENT_GROUP_CREATE,
    RECLAIM_EVENT_PAGE_ADD,
    RECLAIM_EVENT_PAGE_ACCESS,
    RECLAIM_EVENT_AGE_GROUP,
    RECLAIM_EVENT_RECLAIM_ALL,
};
```
以及：
```
struct reclaim_event {
    enum reclaim_event_type type;
    uint64_t sequence;
    union {
        ...
    } data;
};
```
它解决的问题是：
```
文本trace
C语言测试
未来内核事件
```
最终都转换成统一的 `reclaim_event`。
这样核心引擎不需要关心事件原来来自文件、测试代码还是内核 hook。

#### **types.h**
定义项目通用的基础类型、枚举和常量。
例如：
```
enum reclaim_page_type {
    RECLAIM_PAGE_ANON,
    RECLAIM_PAGE_FILE,
};

enum reclaim_page_state {
    RECLAIM_PAGE_NEW,
    RECLAIM_PAGE_ON_LRU,
    RECLAIM_PAGE_ISOLATED,
    RECLAIM_PAGE_UNEVICTABLE,
};

enum reclaim_lru_kind {
    RECLAIM_LRU_INACTIVE_ANON,
    RECLAIM_LRU_ACTIVE_ANON,
    RECLAIM_LRU_INACTIVE_FILE,
    RECLAIM_LRU_ACTIVE_FILE,
};
```
还可能包含：
```
typedef uint64_t reclaim_page_id_t;
typedef uint64_t reclaim_cgroup_id_t;
```
它是多个模块共同依赖的基础定义文件。
原则上不应把复杂函数声明全部塞进这里。

#### **error.h**
统一错误码。
例如：
```
enum reclaim_error {
    RECLAIM_OK = 0,
    RECLAIM_ERR_INVALID_ARGUMENT,
    RECLAIM_ERR_NO_MEMORY,
    RECLAIM_ERR_PAGE_NOT_FOUND,
    RECLAIM_ERR_DOMAIN_NOT_FOUND,
    RECLAIM_ERR_VALIDATION,
};
```
还可以声明：
```
const char *reclaim_error_string(int error);
```
作用是统一所有模块的错误表达，避免：
```
page.c 返回 -1
domain.c 返回 1
parser.c 返回 100
```
这种混乱。

#### **platform.h**
定义平台抽象接口。
它解决用户态、Linux 内核、OpenHarmony 内核函数不同的问题。
例如：
```
struct reclaim_allocator_ops {
    void *(*alloc)(void *context, size_t size);
    void *(*calloc)(void *context, size_t count, size_t size);
    void (*dealloc)(void *context, void *pointer);
};
```
以及：
```
struct reclaim_clock_ops;
struct reclaim_log_ops;
struct reclaim_lock_ops;
```
用户态映射为：
```
malloc / calloc / free
clock_gettime
fprintf
空锁
```
未来内核映射为：
```
内核分配接口
内核时钟
内核日志
spinlock/mutex
```
因此 `src/core/` 不直接依赖具体平台。


#### **policy.h**
定义可替换的策略接口。
主要包括：
```
struct reclaim_aging_ops;
struct reclaim_domain_selector_ops;
struct reclaim_scan_policy_ops;
```
例如：
```
struct reclaim_aging_ops {
    int (*page_access)(...);
    int (*age_domain)(...);
};
```
第一版使用：
```
G1简化老化策略
```
后续可新增：
```
Linux-like aging
MGLRU aging
预测驱动策略
```
而不修改引擎总框架。

#### **executor.h**
定义“候选页面如何执行回收”的接口。
例如：
```
struct reclaim_executor_ops {
    int (*execute_batch)(
        void *context,
        struct reclaim_candidate_batch *batch,
        struct reclaim_exec_result *result);
};
```
第一阶段由：
```
simulator_executor.c
```
实现。
未来由：
```
Linux executor
OpenHarmony executor
```
实现。
它是策略层和真实回收机制之间的边界。

#### **stats.h**
定义各种统计结构。
例如：
```
struct reclaim_engine_stats;
struct reclaim_domain_stats;
struct reclaim_exec_result;
struct reclaim_result;
```
统计可能包括：
```
扫描folio数量
扫描基础页数量
隔离数量
成功回收数量
putback数量
激活数量
dirty/writeback/busy数量
overshoot数量
最终priority
停止原因
```
它负责“记录发生了什么”，不负责决定策略。


**validator.h**
定义一致性检查接口。
核心接口类似：
```
int reclaim_engine_validate(
    const struct reclaim_engine *engine,
    struct reclaim_validation_report *report);
```
它用于检查：
- 页面是否重复挂链；
- 页面状态是否和 LRU 位置一致；
- cgroup 所有权是否正确；
- 哈希表和链表是否一致；
- 统计量是否守恒。
测试模式下，每处理一个事件都可以调用一次。

