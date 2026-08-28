# Kernel adapter design

本阶段只定义适配契约，不修改 Linux 源码。

`kernel/adapters/parp_snapshot_adapter.py` 将用户态快照转换为有限的 PARP snapshot 风格提示，保留 `prediction_seq`、TTL、Q15、scope 和回退状态。它明确输出 `apply: false`。

未来真正对接时，应优先使用现有 v4.2 的版本化 `/dev/myfs` atomic state ABI；debugfs `app_bind`/`app_prior` 只作为历史或审计兼容面。Workload 状态需要新的版本化扩展字段，不能假装是 App prior，也不能把区域地址直接传入 reclaim 热路径。

内核消费者必须在 TTL、序列号、ABI、scope 和置信度检查失败时使用 Native。内核 reclaim/MGLRU 上下文禁止模型推理、阻塞 I/O、重分配和复杂区域遍历。任何真实页级 tier、scan budget、预清洗、压缩或迁移 APPLY 都需要独立补丁、KUnit、锁上下文审查和授权实验。
