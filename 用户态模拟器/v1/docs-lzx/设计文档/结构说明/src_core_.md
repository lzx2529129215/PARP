# 平台无关核心实现

这个目录是项目最核心的部分。
原则是：
> 不依赖用户态命令行，不依赖具体内核，不直接读 trace 文件。

 #### **engine.c**
实现 `engine.h` 中的总控逻辑。
主要负责：
- 创建和销毁引擎；
- 保存配置和 ops；
- 初始化哈希表；
- 初始化统计；
- 分发事件；
- 调用 page/domain/aging/reclaim 等模块；
- 管理 `event_seq`；
- 统一错误返回。
它更像“调度员”，不应包含所有 LRU、哈希和回收细节。
例如：
```
收到 PAGE_ACCESS
→ 查找页面
→ 调用 aging_g1

收到 RECLAIM_ALL
→ 调用 reclaim.c
```
#### **page.c**
负责单个 folio 元数据的生命周期。
主要包括：
- 创建页面；
- 删除页面；
- 访问页面；
- recharge；
- migrate；
- 查询页面；
- 计算 `1ULL << order`；
- 更新页面状态。
例如：
```
int reclaim_page_add(...);
int reclaim_page_remove(...);
int reclaim_page_recharge(...);
```
它关注“页面本身”，不负责整个全局回收流程。

#### **domain.c**
负责 cgroup/domain 管理。
一个 domain 代表一个 cgroup 的 Shadow LRU 空间。
主要包括：
- 创建 cgroup；
- 销毁 cgroup；
- 初始化四条 LRU；
- 设置 swappiness；
- 设置 swap_enabled；
- 获取继承后的最终配置；
- 维护 domain 统计。
例如：
```
int reclaim_domain_create(...);
int reclaim_domain_destroy(...);
```
#### **hash.c**
实现页面和 cgroup 的哈希索引。
主要用于：
```
page_id → reclaim_page
cgroup_id → reclaim_domain
```
包含：
- 初始化哈希桶；
- 插入；
- 查找；
- 删除；
- 销毁；
- 处理冲突链。
它不理解 LRU 策略，只负责快速定位对象。

#### **list.c**
实现通用侵入式双向链表。
“侵入式”表示链表节点直接嵌在页面结构中：
```
struct reclaim_page {
    ...
    struct reclaim_list_node lru_node;
};
```
它通常提供：
```
list_init();
list_add_head();
list_add_tail();
list_remove();
list_move_tail();
list_empty();
```
该模块只处理指针和链表，不理解 anon/file、cgroup 或回收语义。

#### lru.c
实现四条 LRU 的具体操作。
主要负责：
- 将页面加入指定 LRU；
- 从 LRU 删除；
- active/inactive 迁移；
- anon/file 类型检查；
- 更新 LRU 统计；
- 从链表头部选择候选；
- 防止重复挂链。
例如：
```
int reclaim_lru_add(...);
int reclaim_lru_remove(...);
int reclaim_lru_move(...);
```
`list.c` 是通用链表工具，`lru.c` 是带内存管理语义的链表操作。

#### **aging_g1.c**
实现第一版简化老化策略 G1。
负责：
```
PAGE_ACCESS
→ 设置 referenced

AGE_GROUP / AGE_ALL
→ 根据 referenced 执行 active/inactive 转换
```
规则是：
```
inactive + referenced → active
active + 未引用       → inactive
active + referenced   → 刷新到active尾部
inactive + 未引用     → 保持
```
它通过 `reclaim_aging_ops` 注册给引擎。
后续实现 Linux-like aging 时，可以新增：
```
aging_linux_like.c
```
而不是重写 `engine.c`。

#### scan_pressure.c
负责计算“本轮应该扫描多少页面”。
包含：
- priority 计算；
- `effective_lru_pages >> priority`；
- 最少扫描一页；
- swappiness 权重；
- swap_enabled；
- anon/file 预算分配；
- 某类不足时预算转移；
- 批次大小限制。
它不真正移动页面，只计算预算。
例如输出：
```
当前domain总扫描预算：32页
anon预算：10页
file预算：22页
```
#### **reclaim.c**
负责完整的回收主流程。
这是核心回收状态机：
```
收到回收请求
→ 计算扫描压力
→ 扫描inactive链
→ 隔离候选
→ 构建batch
→ 调用executor
→ 成功删除
→ 失败putback
→ 更新统计
→ 判断是否继续下一priority
```
它实现：
- `RECLAIM_GROUP`；
- `RECLAIM_ALL`；
- priority 轮次；
- 达到目标停止；
- 无进展停止；
- executor 错误恢复；
- overshoot；
- candidate batch 回滚。
它是整个项目最关键的业务文件之一。

#### **stats.c**
实现统计累加、清理和输出辅助。
例如：
```
reclaim_stats_add_scanned(...);
reclaim_stats_add_reclaimed(...);
reclaim_stats_reset_result(...);
```
把统计单独拆出来，可以避免 `reclaim.c` 中到处手写：
```
stats->nr_pages_scanned += ...
```
同时便于统一处理 folio 数和基础页数。

#### **validator.c**
实现所有一致性检查。
它会遍历：
- 页面哈希表；
- domain 哈希表；
- 每条 LRU；
- isolated 状态；
- unevictable 状态；
- 统计量。
检查例如：
```
page 1001:
state = ISOLATED
但仍在 inactive_file
→ 错误
```
或：
```
page 1002:
charge_cgroup_id = 1
但挂在 cgroup 2 的 LRU
→ 错误
```
它是调试链表和状态机错误的重要工具。
