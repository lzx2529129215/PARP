# PARP effective-tier r11 三个修改源码文件说明

本目录随附说明只针对 r11 内核实际修改的三个源码文件，不包含内核安装、GRUB、
构建目录迁移或完整内核源码交付说明。

## 文件清单

```text
include/linux/parp.h
mm/parp/core/effective_tier.c
mm/parp/tests/parp_test.c
```

这三个文件来自构建以下内核时使用的源码工作树：

```text
6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
```

源码工作树原始目录名仍包含 `apply-r9`，但文件内容已经包含后续 r10 和 r11 的修改。
不要根据目录名判断这三个文件的最终版本。

## 修改背景

r9 的 effective-tier Apply 实现为每页 `page_ext` 状态使用 sequence 无锁快照协议。
写者通过奇数 sequence 表示写入中，更新 payload 后再发布偶数 sequence。多个 CPU
并发写同一个状态时，竞争写者可能快速耗尽有限重试，增加 `state_unstable` 并触发
全局 `state_fault`。进入 fail-closed 后，预测 Apply 会停止，因此无法继续取得有效
的预测 ON 数据。

该问题经过两阶段修改：

```text
r9
└── r10：禁止抢占、增加重试和故障诊断
    └── r11：使用哈希自旋锁实现真正的多写者互斥
```

## include/linux/parp.h

该文件增加：

```c
bool parp_effective_tier_state_faulted(void);
```

用途是向测试代码暴露 effective-tier 是否已经进入全局 fail-closed 状态，使并发测试
能够显式检查：

- 正常竞争后不得错误进入 `state_fault`；
- 故意制造协议异常时必须进入 fail-closed；
- OFF → active 的状态切换应按设计清除 fault。

## mm/parp/core/effective_tier.c

这是 r10/r11 并发修复的核心文件。

### r10 阶段

r10 主要加入：

- 写者取得奇数 sequence 后保持禁止抢占，直到 payload 更新完成并发布偶数 sequence；
- 写入失败路径恢复抢占状态，避免 preempt count 泄漏；
- 写者竞争重试由 4 次增加到 64 次；
- 新增 `state_lock_failures` 统计；
- 新增 `parp_effective_tier_state_faulted()`；
- 保留重试耗尽后触发 `state_fault` 的 fail-closed 行为。

r10 仍不能解决不同 CPU 上的竞争：64 次重试是紧密原子循环，竞争 CPU 可能在持有者
发布偶数 sequence 前耗尽重试。因此 r10 实机测试仍出现一次锁失败和全局 fault。

### r11 阶段

r11 将多写者协议改为哈希自旋锁保护：

```c
#define PARP_TIER_STATE_LOCK_BITS 8
#define PARP_TIER_STATE_LOCKS     (1U << PARP_TIER_STATE_LOCK_BITS)
```

即建立 256 个静态 `spinlock_t`，通过 `page_ext` 地址哈希选择锁。主要行为是：

1. 写者调用 `spin_lock_irqsave()` 获得对应哈希锁；
2. 检查当前 sequence 是否为稳定偶数；
3. 发布奇数 sequence；
4. 更新 payload；
5. 通过 release store 发布新的偶数 sequence；
6. 调用 `spin_unlock_irqrestore()` 释放锁。

无锁读者继续使用原 sequence 快照协议，不需要获取自旋锁。

r11 将以下写路径统一纳入同一协议：

- 页面访问状态记录；
- generation move 和迁移状态记录；
- 候选特征、epoch 和 sequence 更新；
- pending action 与 outcome 更新；
- `page_ext` 状态初始化和清零。

如果写者持有哈希锁后仍观察到奇数 sequence，则视为状态协议损坏：增加
`state_lock_failures` 并保留 fail-closed，而不是静默继续。

该实现没有增加逐页元数据：每页 `page_ext` 仍为 24 字节；新增的是约 1 KiB 的
全局锁表。哈希碰撞可能降低无关页面写入的并行度，但不会混合不同页面的 payload。

## mm/parp/tests/parp_test.c

该文件增加 effective-tier 状态协议的并发和安全测试，主要包括：

- 4 个 kthread 并发操作同一个 folio；
- 每个线程最多执行 20,000 次；
- 混合执行 access、generation move、outcome 和无锁 snapshot；
- 检查正常并发结束后 `state_fault` 保持为 false；
- 检查状态快照和 payload 不出现不可接受的撕裂；
- 显式构造错误状态，验证 fail-closed 能够触发；
- 验证模式切换能够按设计清除 fault。

这些测试用于验证锁协议和安全门，不用于证明预测模型具有性能收益。

## 修改后的行为边界

r11 解决的是“多个写者竞争导致重试耗尽并进入全局 fail-closed”的问题。在对应
hotcold smoke/full 测量中：

```text
state_fault=0
state_lock_failures=0
model_invalid=0
PARP decision/access/outcome 持续非零
```

但 r11 没有证明或保证：

- `metadata_missing` 已经消除；
- `state_unstable` 已经完全消除；
- 未训练模型能够降低 PageFault；
- 哈希锁没有性能成本；
- `PROTECT_ONLY` 等同于完整的升级/降级策略。

测试使用的模型为 `ENGINEERING_FIXTURE_UNTRAINED`。它只用于验证机制链路，不能称为
正式训练模型，也不能用当前结果宣称正式预测收益。

## 使用这三个文件时的注意事项

- 三个文件必须基于与原工作树一致的 Linux 6.17.13 PARP 源码使用，不能直接覆盖
  任意上游 Linux 6.17.13 源码；它们依赖现有 PARP 类型、Kconfig 和其他模块接口。
- 三个文件包含当前工作树最终状态，不是两个独立补丁；若需要审核 r9→r10→r11 的
  精确差异，应另外生成两份按基线拆分的 patch。
- 文件中的固定 PID、实验目录或运行轮次不属于内核分类规则。
- 在重新构建前应保存源码基线、`.config` 和文件哈希，以便确认输入一致。

## 文件作用摘要

| 文件 | 作用 |
|---|---|
| `include/linux/parp.h` | 暴露 state fault 查询接口供内核测试使用 |
| `mm/parp/core/effective_tier.c` | 实现 r10 诊断增强及 r11 哈希自旋锁写者互斥 |
| `mm/parp/tests/parp_test.c` | 增加同 folio 多写者竞争、快照和 fail-closed 测试 |

这三个文件构成当前 r11 并发状态协议修改的最小源码交付集合。
