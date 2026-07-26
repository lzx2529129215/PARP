# `basic.trace` 中文说明

## 1. 用途

这个 trace 是最小基础场景，用于演示：

- 创建两个 cgroup domain；
- 添加 anonymous 和 file folio；
- 跨 cgroup 访问不改变 charge owner；
- `AGE_GROUP` 将被访问的 inactive anon folio 激活；
- `SUCCESS` 执行结果删除 folio 元数据；
- folio 不可拆分导致基础页回收量超过目标；
- `DUMP` 和 `VALIDATE`。

## 2. 原始 trace

```text
GROUP_CREATE 1
GROUP_CREATE 2
PAGE_ADD 10 1 ANON 0
PAGE_ADD 20 1 FILE 2
PAGE_ADD 30 2 FILE 0
PAGE_ACCESS 10 2
AGE_GROUP 1
PAGE_EXEC_OUTCOME 20 SUCCESS
RECLAIM_GROUP 1 1
DUMP
VALIDATE
```

## 3. 逐行解释

| 行 | 事件 | 中文解释 |
|---:|---|---|
| 1 | `GROUP_CREATE 1` | 创建 cgroup 1，并初始化它的四条 Shadow LRU 链。 |
| 2 | `GROUP_CREATE 2` | 创建 cgroup 2；全局老化和回收按 cgroup ID 升序处理。 |
| 3 | `PAGE_ADD 10 1 ANON 0` | 向 cgroup 1 添加 page 10。它是 order 0 的 anonymous folio，占 1 个基础页，初始进入 `INACTIVE_ANON`。 |
| 4 | `PAGE_ADD 20 1 FILE 2` | 向 cgroup 1 添加 page 20。它是 order 2 的 file folio，占 `1 << 2 = 4` 个基础页，初始进入 `INACTIVE_FILE`。 |
| 5 | `PAGE_ADD 30 2 FILE 0` | 向 cgroup 2 添加 page 30，占 1 个基础页，进入 cgroup 2 的 `INACTIVE_FILE`。 |
| 6 | `PAGE_ACCESS 10 2` | cgroup 2 访问 page 10。页面设置 `referenced=true`、`shared=true`，但 `charge_cgroup_id` 仍为 1，不会迁移到 cgroup 2。 |
| 7 | `AGE_GROUP 1` | 对 cgroup 1 执行 G1 老化。page 10 因被引用而移动到 `ACTIVE_ANON`；page 20 未被引用，继续留在 `INACTIVE_FILE`。 |
| 8 | `PAGE_EXEC_OUTCOME 20 SUCCESS` | 为 page 20 的下一次模拟执行注入 SUCCESS，该注入只消费一次。 |
| 9 | `RECLAIM_GROUP 1 1` | 只回收 cgroup 1，目标为 1 个基础页。page 10 已经 active，不是候选；page 20 被隔离并成功回收。由于 page 20 是 4 基础页的不可拆分 folio，实际回收 4 页，overshoot 为 3 页。 |
| 10 | `DUMP` | 按固定顺序输出 domain 和剩余页面。page 20 已从哈希表删除，因此不会出现在输出中。 |
| 11 | `VALIDATE` | 检查哈希表、LRU 链、生命周期、owner 和统计是否守恒。 |

## 4. 关键状态变化

执行回收前：

```text
cgroup 1:
  page 10 -> ACTIVE_ANON, 1 基础页
  page 20 -> INACTIVE_FILE, 4 基础页
cgroup 2:
  page 30 -> INACTIVE_FILE, 1 基础页
```

执行 `RECLAIM_GROUP 1 1` 后：

```text
page 10 -> 仍存在，owner=1，状态为 ON_LRU，LRU 为 ACTIVE_ANON
page 20 -> 成功回收，从索引和 LRU 中删除
page 30 -> 不受定向回收影响，仍属于 cgroup 2
```

## 5. 运行命令

在 `v1` 根目录执行：

```sh
./output/build-debug/bin/reclaim_simulator \
  --validate-each-event \
  --validate-at-end \
  examples/basic.trace
```

也可以使用标准输入：

```sh
./output/build-debug/bin/reclaim_simulator --validate-each-event \
  < examples/basic.trace
```

## 6. 预期结果

`DUMP` 应包含 cgroup 1、cgroup 2、page 10 和 page 30，不应包含 page 20。page 10 应为 `ACTIVE_ANON`，page 30 应为 `INACTIVE_FILE`。最后的 `VALIDATE` 应成功，程序退出码应为 0。
