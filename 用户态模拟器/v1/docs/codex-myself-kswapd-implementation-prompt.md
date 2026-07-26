# Codex 实施任务：从零实现 myself-kswapd 第一阶段用户态模拟器

你是一名负责操作系统内存管理与 C 工程实现的资深工程师。请在以下 Git 仓库中，从零实现第一阶段功能：

- 仓库：https://github.com/lzx2529129215/myself-kswapd.git
- 语言：C11
- 构建系统：CMake
- 测试系统：CTest
- 当前仓库预期为空
- 设计规格路径：`docs/superpowers/specs/2026-07-26-shadow-per-cgroup-lru-simulator-design.md`

如果设计规格文件已存在，以该文件为最高优先级来源；如果该文件尚不存在，则根据本指令创建，并保持本指令中所有语义不变。

---

## 一、强制工作流程

1. 先检查仓库、分支、远端和工作区状态，不要直接写代码。
2. 使用独立 Git worktree 或独立特性分支完成开发，不能污染其他工作区。
3. 在编码前创建详细实施计划：
   - 路径：`docs/superpowers/plans/2026-07-26-shadow-per-cgroup-lru-simulator-implementation.md`
   - 计划必须逐任务列出创建/修改文件、接口、测试、执行命令、预期失败、最小实现和提交点。
   - 禁止出现 `TODO`、`TBD`、“稍后实现”等占位内容。
4. 对实施计划执行自检：
   - 规格覆盖完整；
   - 类型和接口命名一致；
   - 无未定义函数；
   - 无范围外功能；
   - 无占位描述。
5. 按 TDD 实施：先写失败测试，确认失败，再写最小实现使其通过。
6. 每个可独立审查的任务完成后提交一次，提交信息清晰、单一职责。
7. 完成前必须执行完整验证，不能只凭代码阅读宣称完成。
8. 仅在存在规格矛盾、无法安全推断或环境阻塞时询问用户；普通工程选择直接依据本指令作出保守决定。
9. 完成后推送到当前 GitHub 远端；若环境没有凭证或网络受限，保留本地提交并报告准确错误，不得假称已推送。

若已安装 Superpowers skills，依次使用：

- `using-git-worktrees`
- `writing-plans`
- `test-driven-development`
- `verification-before-completion`
- `requesting-code-review`

---

## 二、项目目标

实现一个平台无关、单线程、确定性的纯 C 用户态模拟器，用于验证：

- Shadow per-cgroup LRU 数据结构；
- folio 生命周期；
- active/inactive anon/file 四链 LRU；
- 页面访问与显式老化；
- cgroup 定向回收与全局回收；
- Linux 风格 priority 递增扫描压力；
- swappiness 与 swap 开关；
- 候选隔离、模拟执行、成功删除、失败 putback；
- 统计守恒和逐事件一致性验证；
- 文本事件回放与内置测试。

第一阶段不是内核模块，不回收真实物理内存，不替换真实 kswapd，也不实现 Linux/OpenHarmony 的 swap、writeback、unmap 或物理页释放。

---

## 三、架构原则

### 1. 真实全局 LRU 与 Shadow LRU 的边界

Shadow per-cgroup LRU 是辅助策略索引：

- 按 cgroup 组织 folio；
- 维护策略层冷热顺序；
- 后续用于预测保护和回收优先级；
- 不拥有真实 folio 生命周期；
- 不直接修改真实内核全局 LRU；
- 后续内核阶段只提供“优先尝试、允许隔离、暂时保护”的建议。

真实全局 LRU 和原有 vmscan/reclaim 路径始终是页面事实状态和真实执行的权威来源。

用户态第一阶段不存在真实全局 LRU，因此 per-cgroup LRU 用作完整的策略状态机测试结构。

### 2. 无全局可变状态

所有状态都属于 `struct reclaim_engine` 实例。公开函数显式接收引擎指针。禁止使用可变全局单例。

### 3. 平台抽象

核心层不能直接调用：

- `malloc/calloc/free`
- `printf/fprintf`
- `clock_gettime`
- `pthread`
- 文件 I/O

核心通过实例级 ops 调用平台能力。用户态适配器可以使用标准库和 POSIX 接口。

至少拆分以下 ops：

```c
struct reclaim_allocator_ops;
struct reclaim_clock_ops;
struct reclaim_log_ops;
struct reclaim_lock_ops;
struct reclaim_executor_ops;
```

第一版：

- allocator → `malloc/calloc/free` 包装；
- clock → 用户态单调时钟或确定性逻辑时间；
- logger → stdout/stderr 包装；
- locks → 单线程空实现；
- executor → 模拟回收执行器。

### 4. 零第三方依赖

除 C 标准库、必要的 POSIX 用户态接口、CMake 和 CTest 外，不引入第三方库。测试使用自带轻量测试框架。

---

## 四、构建目标与仓库结构

必须至少生成：

```text
reclaim_core        # 平台无关静态库
reclaim_simulator   # 文本事件回放 CLI
reclaim_tests       # 单元、组件和场景测试
```

建议结构：

```text
myself-kswapd/
├── CMakeLists.txt
├── README.md
├── LICENSE
├── include/myself_kswapd/
│   ├── engine.h
│   ├── event.h
│   ├── types.h
│   ├── error.h
│   ├── platform.h
│   ├── policy.h
│   ├── executor.h
│   ├── stats.h
│   └── validator.h
├── src/core/
│   ├── engine.c
│   ├── page.c
│   ├── domain.c
│   ├── hash.c
│   ├── list.c
│   ├── lru.c
│   ├── aging_g1.c
│   ├── scan_pressure.c
│   ├── reclaim.c
│   ├── stats.c
│   └── validator.c
├── src/simulator/
│   ├── main.c
│   ├── event_parser.c
│   ├── event_runner.c
│   ├── userspace_platform.c
│   └── simulator_executor.c
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── scenarios/
│   └── test_support/
├── adapters/
│   ├── linux/README.md
│   └── openharmony/README.md
└── docs/
    ├── architecture.md
    ├── event-format.md
    ├── porting.md
    └── superpowers/
        ├── specs/
        └── plans/
```

允许在不改变职责边界的前提下微调文件拆分，但每个文件必须单一职责，不能形成超大 `engine.c`。

---

## 五、核心数据语义

### 1. Folio 对象

一个 `reclaim_page` 表示一个不可拆分的 folio：

```c
struct reclaim_page {
    uint64_t page_id;
    uint64_t charge_cgroup_id;
    uint64_t last_access_cgroup_id;

    enum reclaim_page_type type;
    enum reclaim_page_state state;
    enum reclaim_lru_kind lru_kind;

    uint32_t order;
    uint32_t flags;

    uint64_t last_access_seq;
    uint64_t last_age_seq;
    uint32_t access_count;

    bool referenced;
    bool shared;

    enum reclaim_sim_outcome next_sim_outcome;

    struct reclaim_list_node lru_node;
    struct reclaim_page *hash_next;
};
```

基础页数量：

```c
nr_base_pages = 1ULL << order;
```

所有目标、预算和统计统一按 4 KiB 基础页计数，同时保留 folio 数统计。

第一版不拆分 folio。最后一个 folio 可以导致扫描量或回收量超过目标，记录：

```c
nr_overshoot_pages
```

### 2. 单一 cgroup 所有权

采用 F1：

- 谁完成页面 charge，页面就归属于哪个 cgroup；
- `charge_cgroup_id` 决定页面进入哪个 Shadow per-cgroup LRU；
- 其他 cgroup 访问共享页只更新：
  - `last_access_cgroup_id`
  - `shared=true`
- 普通访问不改变 `charge_cgroup_id`；
- 只有显式 recharge 操作可改变所有者。

### 3. 页面索引和挂链

- `page_id` 通过哈希表平均 O(1) 查询；
- cgroup 也通过哈希表查询；
- 页面结构内嵌一个 LRU 节点；
- 同一页面最多挂在一条普通 LRU 中；
- 页面不能通过复制节点同时出现在多个 cgroup LRU。

### 4. 生命周期

```c
enum reclaim_page_state {
    RECLAIM_PAGE_NEW,
    RECLAIM_PAGE_ON_LRU,
    RECLAIM_PAGE_ISOLATED,
    RECLAIM_PAGE_UNEVICTABLE,
};
```

合法转换：

```text
PAGE_ADD:
NEW → ON_LRU

隔离:
ON_LRU → ISOLATED

失败 putback:
ISOLATED → ON_LRU

重新激活:
ISOLATED → ON_LRU，并进入 active 链

不可回收:
ISOLATED → UNEVICTABLE

成功回收:
ISOLATED → 从哈希表删除并释放元数据
```

成功回收后不保留 `RECLAIMED` 元数据对象。

---

## 六、每 cgroup 四链 LRU

每个 domain 至少包含：

```text
inactive_anon
active_anon
inactive_file
active_file
```

新 folio 默认进入对应类型的 inactive 尾部。

暂不要求独立 unevictable 链；不可回收页面可以通过生命周期状态和独立索引/统计管理，但必须从普通四链中移除。

---

## 七、G1 简化老化策略

第一版明确标记为 `V1_SIMPLIFIED_AGING`，不得宣称完整复现 Linux。

### PAGE_ACCESS

只执行：

```text
referenced = true
last_access_seq = engine->event_seq
last_access_cgroup_id = accessing_cgroup_id
access_count++
若访问者不是 charge cgroup，则 shared=true
```

访问事件本身不立即修改 LRU 链。

### AGE_GROUP / AGE_ALL

规则：

```text
inactive + referenced → active 尾部
inactive + 未引用    → 保持原位置
active + referenced   → active 尾部刷新
active + 未引用       → inactive 尾部
处理后 referenced=false
更新 last_age_seq
```

同时支持：

```text
AGE_GROUP <cgroup_id>
AGE_ALL
```

`AGE_ALL` 必须按 `cgroup_id` 稳定顺序处理。

通过 `reclaim_aging_ops` 抽象，后续可增加更接近 Linux 的传统 LRU 老化和 MGLRU 策略，但第一阶段不实现。

---

## 八、回收请求与扫描压力

支持：

```text
RECLAIM_GROUP <cgroup_id> <target_base_pages>
RECLAIM_ALL <target_base_pages>
```

### 1. 定向回收

只扫描目标 cgroup，不能修改其他 cgroup 的页面或统计。

### 2. 全局回收

采用 K1-L：简化 Linux 式动态扫描压力，不使用静态全局 20/10/5 比例表。

默认配置：

```c
default_priority = 12;
minimum_priority = 0;
scan_batch_pages = 32;
max_reclaim_rounds = default_priority - minimum_priority + 1;
```

测试可以覆盖为较小值。

基础扫描量：

```c
scan_pages = effective_lru_pages >> priority;
```

若结果为 0，但存在可扫描页且目标未达到：

```c
scan_pages = 1;
```

每个 priority：

1. 按 `cgroup_id` 稳定遍历全部 domain；
2. 对每个 domain 现场计算扫描量；
3. 按批次扫描；
4. 累计扫描与回收结果；
5. 达到目标立即停止；
6. 否则 priority 递减后进入下一轮。

终止原因至少包括：

```c
RECLAIM_STOP_TARGET_REACHED
RECLAIM_STOP_NO_SCANNABLE_PAGES
RECLAIM_STOP_NO_PROGRESS
RECLAIM_STOP_PRIORITY_EXHAUSTED
RECLAIM_STOP_EXECUTOR_ERROR
RECLAIM_STOP_ROUND_LIMIT
```

不得无限重复扫描 BUSY/dirty 页面。

---

## 九、swappiness 与 swap

采用全局默认值 + per-cgroup 可选覆盖。

默认：

```c
default_swappiness = 60;   /* 合法范围 0..200 */
default_swap_enabled = true;
```

domain 可分别覆盖：

```c
override_swappiness
swappiness

override_swap_enabled
swap_enabled
```

预算模型：

```text
swap_enabled=false:
    anon_budget=0
    全部预算给 file

swap_enabled=true:
    anon_weight=swappiness
    file_weight=200-swappiness
```

某类页面不足时，未使用预算转移给另一类。

第一版只实现静态权重；不实现 refault、历史回收成本、dirty/writeback 比例和 swap I/O 压力反馈。

---

## 十、两阶段回收 I1-A

必须将候选选择与真实/模拟执行分离。

### 1. 隔离阶段

```text
ON_LRU
→ 从 Shadow LRU 摘除
→ ISOLATED
→ 加入 candidate batch
```

候选记录来源 LRU，用于失败回滚。

若批次构建失败，已隔离页面全部放回原类别 LRU 尾部。

### 2. 执行阶段

统一 executor 接口按批次工作，结果至少包含：

```c
struct reclaim_exec_result {
    int error;

    uint64_t nr_requested;
    uint64_t nr_isolated;
    uint64_t nr_reclaimed;
    uint64_t nr_putback;
    uint64_t nr_activated;

    uint64_t nr_dirty;
    uint64_t nr_writeback;
    uint64_t nr_unevictable;
    uint64_t nr_busy;
};
```

`error=0` 表示执行器正常运行，不表示所有页面都成功回收。

部分回收是正常结果。

### 3. 模拟执行器 P1

默认所有页面：

```text
SUCCESS
```

测试可以为下一次执行单次注入：

```text
SUCCESS
PUTBACK
ACTIVATE
BUSY
DIRTY
WRITEBACK
UNEVICTABLE
```

注入结果消费一次后恢复为 `SUCCESS`。

处理：

```text
SUCCESS      → 删除哈希项、更新统计、释放元数据
PUTBACK      → inactive 尾部
BUSY         → inactive 尾部，统计 busy
DIRTY        → inactive 尾部，统计 dirty
WRITEBACK    → inactive 尾部，统计 writeback
ACTIVATE     → active 尾部
UNEVICTABLE  → 移出普通四链，状态设为 UNEVICTABLE
```

用户态模拟器不尝试伪造 Linux swap、writeback、unmap 和 refcount 逻辑。

---

## 十一、事件系统

文本解析器与 C 测试必须调用同一套公开 API。

至少支持以下事件；具体拼写必须在 `docs/event-format.md` 固定并测试：

```text
GROUP_CREATE <cgroup_id>
GROUP_DESTROY <cgroup_id>

GROUP_SET_SWAPPINESS <cgroup_id> <0..200|INHERIT>
GROUP_SET_SWAP_ENABLED <cgroup_id> <0|1|INHERIT>

PAGE_ADD <page_id> <cgroup_id> <ANON|FILE> <order>
PAGE_ACCESS <page_id> <access_cgroup_id>
PAGE_REMOVE <page_id>
PAGE_RECHARGE <page_id> <new_cgroup_id>
PAGE_MIGRATE <old_page_id> <new_page_id>

PAGE_EXEC_OUTCOME <page_id> <outcome>

AGE_GROUP <cgroup_id>
AGE_ALL

RECLAIM_GROUP <cgroup_id> <target_base_pages>
RECLAIM_ALL <target_base_pages>

VALIDATE
DUMP
```

场景测试可增加只用于测试的断言事件，例如：

```text
ASSERT_PAGE_MISSING <page_id>
ASSERT_PAGE_STATE <page_id> <state>
ASSERT_PAGE_LRU <page_id> <lru_kind>
ASSERT_DOMAIN_PAGES <cgroup_id> <base_pages>
ASSERT_LAST_STOP_REASON <reason>
```

解析错误必须报告文件名、行号、原文本和原因，并停止执行错误事件。第一版不实现 `--continue-on-error`。

---

## 十二、错误与恢复

统一错误码至少覆盖：

```text
INVALID_ARGUMENT
NO_MEMORY
DOMAIN_NOT_FOUND
DOMAIN_ALREADY_EXISTS
DOMAIN_NOT_EMPTY
PAGE_NOT_FOUND
PAGE_ALREADY_EXISTS
PAGE_STATE
PAGE_TYPE
PARSE
EXECUTOR
VALIDATION
NOT_SUPPORTED
INTERNAL
```

原则：

- 输入错误：当前事件不生效；
- 分配失败：状态不改变；
- 批次构建失败：已隔离页面回滚；
- 执行器异常：未完成页面安全 putback；
- 部分回收：正常结果，不是程序错误；
- 一致性破坏：立即终止并报告内部 bug；
- 销毁非空 domain：返回 `DOMAIN_NOT_EMPTY`；
- 引擎销毁时释放所有剩余对象，不得泄漏。

---

## 十三、一致性验证 S1

提供：

```c
int reclaim_engine_validate(
    const struct reclaim_engine *engine,
    struct reclaim_validation_report *report);
```

测试和场景回放默认每个事件后执行。

至少检查：

1. 每个 folio 最多挂一条普通 LRU；
2. ON_LRU 页面必须属于有效 domain；
3. 所在 domain 必须等于 `charge_cgroup_id`；
4. ISOLATED 页面不能在普通 LRU；
5. UNEVICTABLE 页面不能在普通四链；
6. anon/file 类型必须匹配 LRU；
7. active/inactive 状态必须匹配 LRU kind；
8. 哈希表、LRU、生命周期三者一致；
9. 每条 LRU 的 folio 数和基础页数统计正确；
10. domain 汇总与引擎全局统计守恒；
11. 已删除页面不能被索引到；
12. 不允许重复链表节点。

CLI 支持：

```text
--validate-each-event
--validate-at-end
--no-validate
```

测试默认逐事件验证。

---

## 十四、确定性要求

结果不得依赖：

- 墙钟时间；
- 线程调度；
- 指针地址；
- 随机哈希种子；
- 哈希桶遍历顺序。

统一使用：

- `event_seq` 作为逻辑时间；
- `cgroup_id` 作为 domain 稳定排序；
- 明确定义 LRU 头尾；
- 明确定义预算余数分配；
- DUMP 输出字段和顺序固定。

同一 trace 连续运行多次，输出必须逐字节一致。

---

## 十五、测试要求

使用 CTest，至少覆盖：

### 单元测试

- intrusive list；
- page/domain 哈希索引；
- allocator ops；
- 分配失败注入；
- LRU 插入、删除、移动；
- folio order 到基础页换算；
- swappiness 预算；
- priority 扫描量；
-统计累计和 overshoot。

### 集成/场景测试

- 基础页面生命周期；
- anon/file 四链转换；
- 跨 cgroup 访问不改变 charge owner；
- `AGE_GROUP` 不影响其他 domain；
- `AGE_ALL` 稳定顺序；
- `RECLAIM_GROUP` 隔离性；
- `RECLAIM_ALL` priority 递减；
- swap disabled 不扫描 anon；
- swappiness 0/60/200；
- 大 folio 不拆分并允许 overshoot；
- SUCCESS 删除；
- BUSY/DIRTY/WRITEBACK/PUTBACK 正确放回；
- ACTIVATE 返回 active；
- UNEVICTABLE 离开四链；
- 所有页面 BUSY 时最终 NO_PROGRESS；
- 执行器异常时安全回滚；
- 分配失败后状态保持一致；
- 非法事件不产生部分修改；
- 同一 trace 重复执行输出一致；
- 引擎销毁后分配计数归零。

故意破坏内部状态，验证 validator 能发现：

- 重复挂链；
- 统计被篡改；
- ISOLATED 仍在 LRU；
- charge owner 与 domain 不一致；
- 页面类型与 LRU 不一致。

测试专用内部接口不得暴露到正式公共 API。

---

## 十六、编译与验证

CMake 必须支持：

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Debug \
  -DRECLAIM_ENABLE_TESTS=ON \
  -DRECLAIM_ENABLE_SANITIZERS=ON

cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Debug 构建至少启用：

```text
-Wall
-Wextra
-Wpedantic
-Werror
AddressSanitizer
UndefinedBehaviorSanitizer
```

若编译器不支持某个 sanitizer，CMake 必须清晰检测并报告，不能静默生成错误配置。

完成前还要执行：

1. Debug + sanitizers 全套测试；
2. Release 构建和测试；
3. 代表性 trace 连续运行至少两次并比较输出；
4. 检查无 ASan 泄漏、越界、UAF；
5. 检查无 UBSan 报告；
6. 检查 `git status --short`；
7. 检查设计规格和 README 与实现一致；
8. 检查第一阶段没有引入 pthread、Linux 内核头文件或 OpenHarmony 内核依赖。

---

## 十七、明确禁止的范围

第一阶段不要实现：

- Linux 内核模块；
- OpenHarmony 内核补丁；
- 真实物理页回收；
- 真实 swap/writeback/unmap；
- pthread 并发；
- MGLRU；
- 完整 Linux active/inactive 老化；
- LSTM、Markov 和预测模型；
- memory.min / memory.low；
- NUMA node/zone；
- DAMON；
- Tier2/CXL；
- Shadow 与真实全局 LRU 同步 hook。

仅在 `adapters/linux/README.md` 和 `adapters/openharmony/README.md` 记录后续适配边界，不写伪内核实现。

---

## 十八、文档要求

更新或创建：

- `README.md`
  - 项目目的；
  - 当前仅为用户态模拟器；
  - 构建、测试、运行方法；
  - 示例 trace；
  - 明确非真实内核回收器。
- `docs/architecture.md`
  - 模块边界；
  - Shadow LRU 与真实全局 LRU 区别；
  - 数据流和生命周期。
- `docs/event-format.md`
  - 每个事件语法、参数、合法范围、错误语义；
  - 至少一个完整场景。
- `docs/porting.md`
  - platform ops；
  - executor ops；
  - Linux/OpenHarmony 后续适配原则；
  - 不重新实现 swap/writeback/unmap。
- Linux/OpenHarmony adapter README
  - 后续接入步骤；
  - 当前尚未实现；
  - 真实内核状态始终为最终事实来源。

---

## 十九、完成标准

只有同时满足以下条件才可宣称完成：

- 所有 CTest 通过；
- 编译零 warning；
- ASan 无泄漏、越界、UAF；
- UBSan 无错误；
- 所有场景结束后 validator 通过；
- 同一 trace 重复运行输出一致；
- 错误路径不遗留 ISOLATED 页面；
- 引擎销毁后分配计数归零；
- 文档与实际 CLI/接口一致；
- 所有功能均在第一阶段范围内；
- 提交历史清晰；
- 已推送远端，或准确报告无法推送原因。

---

## 二十、最终回复格式

完成后用中文输出：

1. **实现概览**
2. **实际创建/修改的文件**
3. **关键接口和数据流**
4. **测试与验证命令**
5. **每条命令的真实结果摘要**
6. **Git 提交列表**
7. **远端推送状态**
8. **尚未实现且明确属于后续阶段的内容**
9. **发现的设计问题或风险**

禁止只回复“已完成”。禁止隐瞒失败测试、warning、sanitizer 错误或 push 失败。
