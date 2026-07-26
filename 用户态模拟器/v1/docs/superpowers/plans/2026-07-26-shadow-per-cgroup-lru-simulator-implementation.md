# Shadow per-cgroup LRU 模拟器实施计划

## 目标与边界

在 `用户态模拟器/v1` 根目录实现设计规格定义的 C11、单线程、平台无关、确定性用户态模拟器。核心只通过实例级 platform ops、aging ops、executor ops 工作，不直接依赖 C 运行时 I/O、内存分配、时钟、线程或内核接口。模拟器不执行真实物理页回收，不实现真实 swap、writeback、unmap、MGLRU、预测模型、Linux/OpenHarmony 内核代码。

规格优先级为现有 `docs/superpowers/specs/2026-07-26-shadow-per-cgroup-lru-simulator-design.md`，实施 Prompt 与规格一致处全部覆盖。事件接口采用 Prompt 中的 `GROUP_SET_SWAP_ENABLED`，同时解析规格中的 `GROUP_SET_SWAP` 作为兼容别名；文档将 `GROUP_SET_SWAP_ENABLED` 定为规范写法。

## 固定接口与数据约束

- `include/myself_kswapd/types.h` 定义页面类型、页面状态、四种 LRU、模拟执行结果、停止原因、链表节点、页面、domain、候选项、结果和基础计数换算。`order` 超出可安全移位范围时拒绝。
- `include/myself_kswapd/error.h` 定义 `RECLAIM_OK` 与规格列出的错误码，并提供错误码字符串接口。
- `include/myself_kswapd/platform.h` 定义 `reclaim_allocator_ops`、`reclaim_clock_ops`、`reclaim_log_ops`、`reclaim_lock_ops`、`reclaim_platform`。核心所有分配、释放、日志、时钟、锁操作通过 engine 实例调用。
- `include/myself_kswapd/policy.h` 定义 `reclaim_aging_ops`、`reclaim_pressure_config`、扫描预算接口和 G1 策略入口。
- `include/myself_kswapd/executor.h` 定义 `reclaim_executor_ops`、candidate batch、`reclaim_exec_result`，以及用户态 executor 的构造和一次性结果注入接口。
- `include/myself_kswapd/stats.h` 定义 engine/domain 统计和 `reclaim_result`。
- `include/myself_kswapd/validator.h` 定义 `reclaim_validation_report` 与 `reclaim_engine_validate`。
- `include/myself_kswapd/engine.h` 定义 engine 配置和全部公开页面、domain、老化、回收、配置、查询、dump、销毁接口；所有公开操作显式接收 engine 指针。
- `include/myself_kswapd/event.h` 定义事件枚举、解析后的事件结构、事件解析/执行接口和 trace 运行配置。
- 页面采用单一 `charge_cgroup_id` 所有权；跨 domain 访问只更新访问者、引用、共享标记；recharge 才迁移 owner 和 LRU。
- 页面最多存在一个普通 intrusive LRU 节点。`ON_LRU` 必须挂在 owner domain 的匹配四链，`ISOLATED` 不挂普通链，`UNEVICTABLE` 不挂普通链；成功回收立即从索引删除并释放。
- domain 通过 page/domain hash index 查找，并维护按 `cgroup_id` 升序的辅助链，所有全局遍历只使用稳定顺序。
- LRU 头为旧端、尾为新端；PAGE_ADD 和所有 putback/activation 使用尾部；DUMP 按 page_id 排序并排除地址信息。

## 任务 1：构建骨架、公共类型与轻量测试框架

### 文件

- 创建 `CMakeLists.txt`、`README.md`、`LICENSE`。
- 创建 `include/myself_kswapd/{types,error,platform,policy,executor,stats,validator,engine,event}.h`。
- 创建 `src/core/list.c`、`src/core/hash.c`、`src/core/stats.c` 的最小内部实现及对应内部头文件。
- 创建 `tests/test_support/test.h`、`tests/test_support/test.c`、`tests/unit/test_list.c`、`tests/unit/test_types.c`。

### 接口与最小实现

- CMake 生成 `reclaim_core` 静态库、`reclaim_simulator` 和 `reclaim_tests` 目标，开启 C11、Wall/Wextra/Wpedantic/Werror；sanitizer 选项检测编译器支持后为目标添加 ASan/UBSan。
- 轻量测试框架提供断言、失败计数、测试注册和非零失败退出，不引入第三方库。
- intrusive list 提供初始化、空判断、头尾插入、摘除、移动到尾、遍历基础宏/辅助函数，节点包含所属 list 保护字段以便检测重复挂链。
- 基础页换算和枚举字符串接口完成溢出/非法值检查。

### TDD 与验证

1. 先写 list/types 测试并运行 `cmake -S . -B output/build -DRECLAIM_ENABLE_TESTS=ON && cmake --build output/build`，预期因实现缺失而失败。
2. 写最小 list/types 实现，运行 `ctest --test-dir output/build --output-on-failure`，预期通过。
3. 运行 `cmake --build output/build --verbose` 检查无警告；运行 sanitizer 构建。

### 提交点

提交 `test: add list and folio accounting tests` 后提交 `feat: add simulator build and public type skeleton`，每个提交保持单一职责。

## 任务 2：allocator/platform、hash、domain、page 与四链 LRU

### 文件

- 创建 `src/core/{domain,page,lru,engine}.c` 及内部头文件。
- 创建 `src/simulator/userspace_platform.c`。
- 扩充 `tests/unit/{test_hash,test_domain,test_lru,test_platform}.c`。

### 接口与最小实现

- engine create 接受 platform、config、aging、executor；缺省 ops 使用用户态包装，但核心源文件不直接调用标准库分配、输出、时钟或线程。
- page/domain hash 使用确定性 bucket 链和实例 allocator；page_id、cgroup_id 重复检查；domain 销毁前检查空。
- domain 创建四条 LRU 和排序链；page add 预分配 page 并一次性完成索引、owner domain 和 inactive 尾挂链；任何失败回滚到不变状态。
- LRU 迁移维护 page state、kind、链计数、domain 统计和 engine 汇总，统一按基础页与 folio 数计数。
- 用户态 platform 提供 malloc/calloc/free 包装、逻辑时钟、stderr 日志和空锁；另提供可注入分配失败的测试 context。

### TDD 与验证

1. 先写 domain/page/LRU 测试，验证生命周期、四链、重复 ID、非空销毁、order 统计、分配失败状态不变；预期编译或链接失败。
2. 增加最小实现，分别运行 `reclaim_tests` 相关用例和 `ctest`，确认通过。
3. 对每个 page/domain 操作运行 validator；执行 allocator failpoint 测试并确认 tracked allocation 恢复。

### 提交点

提交 `test: cover domain page and lru invariants`，再提交 `feat: implement page domain and lru ownership`。

## 任务 3：访问、G1 老化、配置与扫描预算

### 文件

- 创建 `src/core/aging_g1.c`、`src/core/scan_pressure.c`。
- 扩充 `src/core/engine.c`、`src/core/stats.c` 和 `include` 接口。
- 创建 `tests/unit/{test_aging,test_policy,test_stats}.c`、`tests/integration/test_aging_scope.c`。

### 接口与最小实现

- PAGE_ACCESS 设置 referenced、event_seq、访问 cgroup、access_count；跨 owner 设置 shared，不能移动 LRU。
- G1 按指定 domain 或按升序 domain 处理四链快照：inactive referenced 到 active 尾，active referenced 刷新 active 尾，active 未引用到 inactive 尾，处理后清引用并更新 age seq。
- swappiness 默认 60、范围 0..200；swap disabled 时 anon 预算为 0；启用时按 `swappiness/(200-swappiness)` 分配，某类不足时将剩余预算交给另一类；所有余数按固定顺序处理。
- priority 默认 12、最小 0、batch 32、round 上限为 priority 区间长度；扫描量按 `effective_lru_pages >> priority`，存在候选时至少 1；测试可用小配置。

### TDD 与验证

1. 先写 access/aging/scope、swappiness 0/60/200、swap disabled、priority 和统计测试，预期缺少实现而失败。
2. 实现 G1、预算和压力函数，重复运行单测及 validator。
3. 用两个 domain 验证 AGE_GROUP 不影响其他 domain，AGE_ALL 使用升序稳定顺序。

### 提交点

提交 `test: specify aging and scan pressure semantics`，再提交 `feat: implement g1 aging and swappiness pressure`。

## 任务 4：隔离、模拟执行器、回收与恢复

### 文件

- 创建 `src/core/reclaim.c`。
- 创建 `src/simulator/simulator_executor.c`。
- 扩充 `src/core/engine.c`、`src/core/validator.c`、`include/myself_kswapd/{engine,executor,stats,validator}.h`。
- 创建 `tests/integration/test_reclaim.c`、`tests/integration/test_executor_outcomes.c`、`tests/integration/test_reclaim_failures.c`。

### 接口与最小实现

- 只从 inactive anon/file 选择，依据预算、LRU 顺序和不可拆分 folio 基础页数构造 candidate batch；隔离立即摘链并置 ISOLATED；batch 失败全部安全 putback 到原类别尾部。
- executor 一次处理一批，默认 SUCCESS；注入 outcome 只消费一次并恢复 SUCCESS。SUCCESS 删除 hash/index 并释放 page；PUTBACK/BUSY/DIRTY/WRITEBACK 放 inactive 尾并计数；ACTIVATE 放对应 active 尾；UNEVICTABLE 脱离普通链并置状态。
- `RECLAIM_GROUP` 只访问目标 domain；`RECLAIM_ALL` 各 priority 按升序 domain 遍历，每轮有界、目标达成立即停止、无进展/无候选/priority 耗尽/round 限制/执行错误准确记录。
- 结果分别记录 folio/page scanned、isolated、reclaimed、putback、activated、overshoot、final priority；部分回收仍返回正常。
- executor error 对未完成 isolated 页面全部 putback；任何错误路径不得留下 isolated 页面。

### TDD 与验证

1. 先写回收测试，覆盖 target、定向隔离、global priority、四类 outcome、overshoot、swap、no progress、executor error；预期失败。
2. 先实现 isolation/batch rollback，再实现 executor，再实现 reclaim round；每一小步运行对应测试。
3. 每次回收后执行 validator，检查 domain/page/engine 守恒和无 isolated 泄漏。

### 提交点

提交 `test: define reclaim isolation and executor outcomes`，再提交 `feat: implement two-phase reclaim and rollback`。

## 任务 5：validator、事件解析/回放、CLI 与文档

### 文件

- 完成 `src/core/validator.c`。
- 创建 `src/simulator/{event_parser,event_runner,main}.c`。
- 创建 `docs/{architecture,event-format,porting}.md`、`adapters/linux/README.md`、`adapters/openharmony/README.md`。
- 创建 `tests/scenarios/{test_trace,test_determinism,test_invalid_events}.c` 和 `tests/integration/test_validation_corruption.c`。
- 更新 `README.md` 与 CMake/CTest 注册。

### 接口与最小实现

- validator 检查唯一挂链、owner/domain、state/LRU/type、hash/index/LRU 一致、四链统计、domain/global 守恒和索引删除事实；报告 event_seq、page/domain、规则、期望和观测。
- parser 支持全部规范事件、兼容 `GROUP_SET_SWAP` 别名和场景 ASSERT 事件；解析错误包含文件名、行号、原文、原因且当前事件不生效并停止。
- runner 按事件递增逻辑时间，支持 `--validate-each-event`、`--validate-at-end`、`--no-validate`，固定 DUMP 顺序和稳定文本输出。
- README 固定构建、测试、运行、示例 trace、非内核范围；架构/事件/移植文档与实际命令和接口一致；适配器只写后续边界，不加入内核实现。

### TDD 与验证

1. 先写 parser/runner/scenario 测试，覆盖合法 trace、每种错误、assert、重复运行字节一致；预期失败。
2. 写 parser、runner、CLI 和文档，运行 `reclaim_tests`、CTest 与 CLI trace。
3. 通过测试专用 corruption hook（仅测试编译单元可见）破坏重复挂链、统计、owner、state，确认 validator 拒绝。

### 提交点

提交 `test: add trace replay and validator corruption scenarios`，再提交 `feat: add deterministic simulator cli and documentation`。

## 任务 6：完整验证与审查

### 命令

在独立 worktree 的 `用户态模拟器/v1` 执行：

```sh
cmake -S . -B output/build-debug -DCMAKE_BUILD_TYPE=Debug -DRECLAIM_ENABLE_TESTS=ON -DRECLAIM_ENABLE_SANITIZERS=ON
cmake --build output/build-debug --parallel
ctest --test-dir output/build-debug --output-on-failure
cmake -S . -B output/build-release -DCMAKE_BUILD_TYPE=Release -DRECLAIM_ENABLE_TESTS=ON -DRECLAIM_ENABLE_SANITIZERS=OFF
cmake --build output/build-release --parallel
ctest --test-dir output/build-release --output-on-failure
./output/build-debug/bin/reclaim_simulator --validate-each-event --validate-at-end < representative.trace > output/run-1.out
./output/build-debug/bin/reclaim_simulator --validate-each-event --validate-at-end < representative.trace > output/run-2.out
cmp output/run-1.out output/run-2.out
rg -n 'pthread|linux/|openharmony' include src tests CMakeLists.txt docs README.md adapters
git diff --check
git status --short
```

若 sanitizer 工具链不可用，配置阶段必须明确失败并记录实际诊断，不以未启用 sanitizer 的结果替代。检查实现文件是否落在第一阶段范围，检查 README、事件格式、规格、计划、公共接口、测试结果和提交历史一致。

### 审查清单

- 所有 CTest 通过且无 warning。
- ASan/UBSan 无错误和泄漏；Release 通过。
- 代表性 trace 两次输出逐字节相同。
- 所有错误路径无孤立页，销毁后分配计数为零。
- 无可变全局状态、pthread、内核依赖或越界功能。
- 规格中每项第一阶段目标均有代码和测试映射。
- `git diff --check`、工作区状态和每次提交职责清晰。

### 提交点

最终审查通过后不压扁前述提交；仅对审查修正创建职责明确的补充提交。尝试 `git push -u origin codex/shadow-per-cgroup-lru-v1`，凭证/网络失败时保留本地提交并记录准确错误。

## 预期交付

交付独立分支上的完整源码、测试、文档、计划和本地提交。最终报告逐项列出实际创建/修改文件、命令真实结果、提交列表、远端推送结果、后续阶段排除项和风险；不把历史输出或未执行命令写成当前验证结果。
