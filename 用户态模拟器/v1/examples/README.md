# Trace 示例目录说明

本目录存放用户态模拟器的文本事件 trace。每个 trace 文件都必须配套一个同名的中文说明文件：

```text
<trace-name>.trace
<trace-name>.trace.md
```

例如：`basic.trace` 配套 `basic.trace.md`。

## 配套说明文件要求

每个 `.trace.md` 至少包含：

1. trace 的用途和覆盖的功能；
2. 完整原始 trace 或指向原始文件的链接；
3. 每条事件的中文解释；
4. 关键页面、domain、LRU 和统计状态的预期变化；
5. 运行命令、验证选项和预期结果；
6. 失败注入、overshoot 或停止原因的含义（如果适用）。

新增或修改 trace 时，必须同步新增或修改同名 `.trace.md`。

## 命名建议

- `basic.trace`：基础生命周期和定向回收；
- `aging.trace`：访问与老化；
- `reclaim-outcomes.trace`：executor 各类执行结果；
- `determinism.trace`：重复运行和稳定输出。

trace 命令语法以 `../docs/event-format.md` 为准，运行前建议启用 `--validate-each-event --validate-at-end`。
