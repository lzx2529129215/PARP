# 用户态模拟器

这个目录负责把核心库变成可以直接运行的程序。
它可以依赖：
- 标准输入输出；
- 文件；
- 命令行参数；
- `malloc`；
- 用户态时钟。
但不能把这些依赖反向塞进 `src/core/`。

#### **main.c**
命令行程序入口。
负责解析类似：
```
./reclaim_simulator scenario.trace
```
或者：
```
./reclaim_simulator \
  --validate-each-event \
  --dump-final-state \
  scenario.trace
```
主要流程：
```
解析命令行
→ 创建用户态platform
→ 创建engine
→ 打开trace
→ 调用event_runner
→ 输出结果
→ 销毁engine
```

#### **event_parser.c**
负责把每一行文本解析为 `struct reclaim_event`。
例如：
```
PAGE_ADD 1001 1 FILE 0
```
转换成：
```
event.type = RECLAIM_EVENT_PAGE_ADD;
event.data.page_add.page_id = 1001;
event.data.page_add.cgroup_id = 1;
event.data.page_add.type = RECLAIM_PAGE_FILE;
event.data.page_add.order = 0;
```
它还负责：
- 数字格式检查；
- 枚举文本检查；
- 参数数量检查；
- 行号和错误信息；
- 跳过空行和注释。
它不执行事件。

#### **event_runner.c**
负责按顺序执行解析后的事件。
流程：
```
读一行
→ parser解析
→ engine执行
→ 可选validate
→ 可选dump
→ 继续下一行
```
它还负责：
- 保存最后一次回收结果；
- 执行测试断言事件；
- 出错时报告文件名和行号；
- 根据 CLI 配置决定验证频率。
它是 parser 和 engine 之间的连接层。

#### **userspace_platform.c**
实现用户态平台 ops。
例如：
```
allocator → malloc/calloc/free
logger    → fprintf
clock     → clock_gettime
locks     → 空操作
```
其中：
```
static const struct reclaim_allocator_ops userspace_allocator_ops;
```
会在创建 engine 时注入。
未来内核适配不会使用这个文件。

#### simulator_executor.c
实现模拟回收执行器。
默认：
```
所有候选SUCCESS
```
也支持单次注入：
```
BUSY
DIRTY
WRITEBACK
ACTIVATE
UNEVICTABLE
```
它只决定模拟结果，不负责候选选择。
例如：
```
page1001 next_sim_outcome = BUSY
→ execute_batch时返回BUSY
→ reclaim.c负责putback
```
需要注意：
- executor 报告结果；
- `reclaim.c` 决定如何更新核心状态。
这样避免模拟器实现和核心状态机混在一起。