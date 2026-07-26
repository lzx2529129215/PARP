# 适配边界

可移植核心依赖每个 engine 实例中的操作表：

- allocator：`alloc/calloc/dealloc`；
- clock：单调时钟或逻辑时间源；
- logger：由调用方拥有的日志出口；
- lock：`init/destroy/lock/unlock`；
- executor：批次执行反馈。

v1 用户态适配器使用 C 分配器、确定性的逻辑时间、安静日志器和空锁。Executor 只是模拟器，不执行真实内存管理操作。

## Linux

未来 Linux 适配器必须先将策略查询接入现有全局 LRU 的 vmscan 路径。它必须保持内核 page/folio 状态为权威，并使用已有的隔离、putback、反向映射、writeback、swap 和物理释放机制。本项目不提供内核模块，也不替换内核回收路径。

## OpenHarmony

未来 OpenHarmony 适配器必须在映射操作之前确认目标内核版本、folio/page 结构、cgroup 计费、全局 LRU 调用图和安全的生命周期 hook 点。它必须使用平台原生回收执行机制，不能在本模拟器中重新实现 swap、writeback 或 unmap。
