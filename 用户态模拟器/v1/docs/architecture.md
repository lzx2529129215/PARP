# 架构说明

`reclaim_core` 包含页面/domain 索引、intrusive 链表、生命周期转换、G1 老化、扫描压力、两阶段回收、executor 契约、统计和 validator。所有平台操作都通过每个 engine 实例中的 `struct reclaim_platform` 注入；核心没有可变全局状态。

用户态适配器提供分配器、逻辑时钟、日志器和空锁操作。模拟 executor 默认提供确定性的 SUCCESS，也支持一次性注入 PUTBACK、ACTIVATE、BUSY、DIRTY、WRITEBACK 或 UNEVICTABLE。

## Shadow LRU 边界

四条 per-cgroup 链只在本模拟器中构成完整的策略状态模型。在未来的内核集成中，它们是 Shadow 索引；真实 folio 状态和执行仍以内核全局 LRU 及现有回收路径为权威。Shadow 索引不能直接操作内核全局 LRU 节点。

## 数据流

1. `PAGE_ADD` 分配元数据，通过 page_id 建立索引，将页面 charge 到一个 domain，并追加到匹配的 inactive 链。
2. `PAGE_ACCESS` 只更新引用和访问元数据；`AGE_GROUP` 或 `AGE_ALL` 执行确定性的 G1 转换。
3. 回收计算 priority 压力和 anon/file 预算，隔离 inactive 候选，再将一个批次交给 executor。
4. SUCCESS 删除元数据；失败结果将页面放回、激活或标记为不可回收。Validator 检查哈希索引、链表归属、生命周期状态和统计计数。

folio 不可拆分。所有目标和统计使用 4 KiB 基础页，同时保留 folio 数；当成功回收的 folio 大于剩余目标时记录 overshoot。
