# myself-kswapd 用户态模拟器 v1

这是一个 C11、单线程、确定性的 Shadow per-cgroup LRU 用户态模拟器。它验证 folio 元数据、cgroup owner、active/inactive anon/file 四链、G1 简化老化、priority 扫描压力、swappiness、候选隔离、模拟执行器、putback、统计和一致性验证。

它不是 Linux 内核模块，也不回收真实物理内存；第一阶段不实现真实 swap、writeback、unmap、MGLRU、并发、预测模型或 OpenHarmony/Linux 内核补丁。

## 构建和测试

```sh
cmake -S . -B output/build-debug \
  -DCMAKE_BUILD_TYPE=Debug \
  -DRECLAIM_ENABLE_TESTS=ON \
  -DRECLAIM_ENABLE_SANITIZERS=ON
cmake --build output/build-debug --parallel
ctest --test-dir output/build-debug --output-on-failure
```

也可以构建 Release 版本：

```sh
cmake -S . -B output/build-release -DCMAKE_BUILD_TYPE=Release -DRECLAIM_ENABLE_TESTS=ON
cmake --build output/build-release --parallel
ctest --test-dir output/build-release --output-on-failure
```

构建生成 `reclaim_core`、`reclaim_simulator` 和 `reclaim_tests`。Debug 编译启用 `-Wall -Wextra -Wpedantic -Werror`；sanitizer 配置会检测 AddressSanitizer 和 UndefinedBehaviorSanitizer 支持，不能静默降级。

## 运行

```sh
./output/build-debug/bin/reclaim_simulator --validate-each-event --validate-at-end examples/basic.trace
./output/build-debug/bin/reclaim_simulator --no-validate examples/basic.trace
```

未提供文件时从标准输入读取。默认逐事件及结束时验证；`--no-validate` 关闭验证。`DUMP` 输出按 cgroup_id、page_id 固定排序，不包含地址或时间信息。

## 架构

模块边界和生命周期见 `docs/architecture.md`，事件语法见 `docs/event-format.md`，platform/executor 后续适配边界见 `docs/porting.md`。`docs/superpowers/specs/` 保存设计规格，`docs/superpowers/plans/` 保存实施计划。
