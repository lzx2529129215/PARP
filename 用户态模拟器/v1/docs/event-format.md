# 事件格式

每个非空、非注释行由一个命令和若干空白分隔的参数组成。`#` 表示注释开始。解析采用严格模式：非法行必须报告文件名、行号、原始文本和原因；该事件不会执行，回放随即停止。

## 命令

```text
GROUP_CREATE <cgroup_id>
GROUP_DESTROY <cgroup_id>
GROUP_SET_SWAPPINESS <cgroup_id> <0..200|INHERIT>
GROUP_SET_SWAP_ENABLED <cgroup_id> <0|1|INHERIT>
GROUP_SET_SWAP <cgroup_id> <ON|OFF|INHERIT>       # 兼容别名
PAGE_ADD <page_id> <cgroup_id> <ANON|FILE> <order 0..63>
PAGE_ACCESS <page_id> <access_cgroup_id>
PAGE_REMOVE <page_id>
PAGE_RECHARGE <page_id> <new_cgroup_id>
PAGE_MIGRATE <old_page_id> <new_page_id>
PAGE_EXEC_OUTCOME <page_id> <SUCCESS|PUTBACK|ACTIVATE|BUSY|DIRTY|WRITEBACK|UNEVICTABLE>
AGE_GROUP <cgroup_id>
AGE_ALL
RECLAIM_GROUP <cgroup_id> <target_base_pages>
RECLAIM_ALL <target_base_pages>
VALIDATE
DUMP
```

场景断言为：

```text
ASSERT_PAGE_MISSING <page_id>
ASSERT_PAGE_STATE <page_id> <NEW|ON_LRU|ISOLATED|UNEVICTABLE>
ASSERT_PAGE_LRU <page_id> <NONE|INACTIVE_ANON|ACTIVE_ANON|INACTIVE_FILE|ACTIVE_FILE>
ASSERT_DOMAIN_PAGES <cgroup_id> <base_pages>
ASSERT_LAST_STOP_REASON <TARGET_REACHED|NO_SCANNABLE_PAGES|NO_PROGRESS|PRIORITY_EXHAUSTED|EXECUTOR_ERROR|ROUND_LIMIT>
```

`GROUP_SET_SWAP` 是设计文档中另一种拼写的兼容别名。`PAGE_RECHARGE` 要求页面不是 isolated 状态，并会改变 Shadow owner 和链归属；跨 cgroup 访问不会改变 owner。`PAGE_REMOVE` 拒绝 isolated 页面。销毁非空 domain 会返回错误。

## 完整示例

```text
GROUP_CREATE 10
GROUP_CREATE 20
PAGE_ADD 100 10 ANON 0
PAGE_ADD 200 10 FILE 1
PAGE_ACCESS 100 20
AGE_GROUP 10
PAGE_EXEC_OUTCOME 200 PUTBACK
RECLAIM_GROUP 10 1
ASSERT_PAGE_STATE 200 ON_LRU
DUMP
VALIDATE
```
