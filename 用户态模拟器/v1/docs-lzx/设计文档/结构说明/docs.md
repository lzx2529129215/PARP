# 项目文档

#### docs/architecture.md

完整架构文档。

包括：
- 模块图；
- 核心调用关系；
- Shadow LRU 定义；
- 真实全局 LRU 与 Shadow 的区别；
- folio 生命周期；
- 回收数据流；
- 错误恢复；
- 统计模型。

它回答的是： 整个系统为什么这样设计？

#### docs/event-format.md

文本事件格式说明。
每个事件需要写明：
- 语法；
- 参数；
- 合法范围；
- 前置条件；
- 状态变化；
- 错误情况；
- 示例。
例如：
```
PAGE_ACCESS <page_id> <access_cgroup_id>
```
需要说明：
```
只更新referenced和last_access_cgroup_id
不改变charge_cgroup_id
不立即移动LRU
```

#### docs/porting.md
平台移植说明。
主要解释：
- 哪些代码可以直接复用；
- 哪些 ops 必须重新实现；
- 如何替换 allocator；
- 如何替换 lock；
- 如何替换 executor；
- 用户态与内核态的边界；
- 为什么核心不能直接调用 `malloc`；
- 为什么不重新实现底层内存回收。

#### docs/superpowers/specs/

存放经过确认的设计规格。
例如：
```
2026-07-26-shadow-per-cgroup-lru-simulator-design.md
```
它描述：
> 要实现什么、语义是什么、范围是什么。
规格相对稳定，是实施计划和代码审查的依据。


#### docs/superpowers/plans/

存放具体实施计划。
例如：
```
2026-07-26-shadow-per-cgroup-lru-simulator-implementation.md
```
它描述：
> 按什么顺序创建哪些文件、先写哪些测试、运行什么命令、每步如何提交。
区别是：
```
specs/
= 做什么以及为什么

plans/
= 具体怎么一步步做
```