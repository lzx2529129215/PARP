# Workload-Aware Observe/Shadow Pipeline

## 1. 概述

本文档描述了由 `lzx/lzx-zr/tools/run_workload_aware.py` 实现的用户态 workload-aware 原型的端到端行为。

该脚本的设计目标是处理一串 JSONL 格式的内存访问观测数据，将原始数据转换为面向工作负载的特征，确定当前运行时状态，预测下一阶段的工作负载模式，并输出用于审查或后续集成的分析产物。

这个设计刻意将逻辑保持在 Linux 内核之外，不直接操作内核内存结构、调试文件或调度器内部状态，而是基于已经采集好的观测记录在用户态完成分析，并最终生成 shadow-style 的预测快照用于分析。

---

## 2. 原型目标

该原型有两个核心目标：

1. 观察并识别近期内存访问行为的模式。
2. 使用轻量级的状态驱动模型预测下一阶段或下一种工作负载模式。

整个处理流程围绕以下几个概念阶段展开：

- 输入加载与规范化
- 特征提取
- 工作负载状态演化
- 转移学习
- 预测生成
- 快照写出与兼容性提示

---

## 3. 详细执行流程

### 3.1 输入加载与规范化

脚本会首先解析命令行参数：

- `--input`：输入的 JSONL 文件
- `--output-dir`：生成结果的输出目录
- `--mode`：可选 `OBSERVE` 或 `SHADOW`
- `--method`：可选 `rule_trend` 或 `second_order_markov`

随后调用 `load_observations(path)`。

这个函数会逐行读取 JSONL 输入文件中的非空数据，并将每一行转换为一个 `Observation` 对象。这个转换非常关键，因为后续逻辑要求字段具有稳定的类型，例如：

- `scope_type`
- `scope_id`
- 时间窗口起止时间
- 采样间隔
- region ID 列表
- region 访问值
- region 时间戳
- counters

加载器使用保守的类型转换方式：

- 字符串统一使用 `str(...)` 做规范化
- 数值字段使用 `int(...)` 或 `float(...)` 转换
- 空值使用默认值兜底
- 元组转换保留序列结构，便于后续特征计算

这样可以让输入处理对缺失字段或局部异常数据更具容错性，避免整个流程因为单条异常行而崩溃。

### 3.2 特征提取

对每个 observation，脚本都会调用：

```python
features = extract_features(observation)
```

上游的特征引擎会把原始访问信息转换成结构化的 workload 表征。这个表示形式不是原始 trace，而是更抽象的特征集合，用于描述：

- 访问强度
- region 局部性
- 时间维度行为
- counters 或运行时信号
- 一个时间窗口内的 workload 形态

这一阶段非常关键，因为后续的预测逻辑依赖规范化后的特征，而不是直接依赖原始事件流。特征向量充当了“观测到的 region 行为”与“工作负载分类”之间的桥梁。

### 3.3 工作负载状态机更新

在特征提取之后，脚本调用：

```python
state = machine.update(features)
```

这里会根据当前特征推导出当前的 workload 状态。状态机负责把计算得到的特征值转换成一个状态标签或状态对象，用于总结当前行为模式。

这个状态通常表示如下几类工作负载阶段：

- 稳定行为
- 突发行为
- 过渡型行为
- 低置信度或 fallback 状态

该状态还携带 confidence 等元信息。在脚本中，后续会检查：

```python
state.confidence_q15 == 0
```

这个条件用于判断是否需要回退到 native 执行路径，或者采用更保守的默认输出。

### 3.4 状态转移观测与历史跟踪

脚本维护一个 `history` 列表，用于存放历史状态。

当已经存在前一个状态时，脚本会执行：

```python
predictor.observe_transition(history[-1], state)
```

这一步是学习过程。预测器会利用前一个状态和当前状态更新内部的转移模型。换句话说，系统会学习 workload 行为在时间上是如何演化的。

这一点很重要，因为状态转移模式往往比单独观测点本身更有信息量。一个突发型 workload 可能逐渐演化成稳定状态，或者某个热 region 的突发模式可能突然转向低活跃窗口。预测器正是通过跟踪这些状态转移来推断下一阶段最可能的状态。

### 3.5 预测生成

脚本根据 `--method` 参数选择不同的预测逻辑：

- `rule_trend` → 使用趋势规则逻辑
- `second_order_markov` → 使用二阶 Markov 转移模型

核心调用方式是：

```python
prediction = predictor.predict_markov(history, state) if args.method == "second_order_markov" else predictor.predict_rule_trend(state)
```

预测器最终返回一个结构化的预测对象，用于描述预期的 workload 演化。

这个预测并不是内核命令，也不是直接执行的内存动作；它更像是对当前观测窗口的分析性预测，用于更上层的决策模块或 shadow 兼容层参考。

### 3.6 快照创建

脚本使用如下方式创建快照：

```python
snapshot = make_snapshot(prediction, features, mode=args.mode, native_fallback=state.confidence_q15 == 0)
```

快照中会整合：

- 预测得到的 workload 结果
- 实际观测到的特征数据
- 执行模式（`OBSERVE` 或 `SHADOW`）
- 低置信度或 native fallback 条件下的回退标记

这一步起到兼容层的作用，使输出看起来像一个 PARP 兼容的快照，即便原型仍然运行在用户态。

### 3.7 产物写出

脚本会写出两个 JSONL 文件：

- `features.jsonl`
- `predictions.jsonl`

对每个处理完成的 observation，都会执行：

```python
json.dump({**features.__dict__, "state": state.__dict__}, feature_file, ensure_ascii=False)
feature_file.write("\n")
```

这样可以把特征集合和状态结果一起保存下来。

随后写出预测快照：

```python
json.dump(snapshot, prediction_file, ensure_ascii=False)
prediction_file.write("\n")
```

这使得预测结果以时间序列的方式保留下来，后续可以用于分析和回放。

### 3.8 最终快照导出与兼容性提示

循环结束后，脚本读取 `predictions.jsonl` 中最后一行预测，并写出最终产物：

- `prediction_snapshot.json`
- `parp_shadow_hint.json`

代码逻辑是：

```python
snapshot = json.loads(last_line)
write_snapshot(args.output_dir / "prediction_snapshot.json", snapshot)
write_hint(args.output_dir / "parp_shadow_hint.json", snapshot)
```

这些最终写出设计用于校验和后续兼容性检查。`parp_shadow_hint.json` 作为轻量级审计提示文件，目的不是直接下沉到内核，而是给上层做兼容性和审计参考。

---

## 4. 输出产物

该原型会在目标输出目录中生成以下文件：

- `features.jsonl`：特征和状态记录
- `predictions.jsonl`：每个观测窗口对应的预测记录
- `prediction_snapshot.json`：最后一次预测生成的最终快照
- `parp_shadow_hint.json`：用于 PARP 风格 shadow 集成的兼容性提示

这些产物有助于：

- 调试 workload 分类器
- 校验状态转移是否合理
- 检查预测质量
- 生成用户态 shadow 输出，用于与 native 行为对比

---

## 5. 运行语义

### OBSERVE 模式

该模式主要用于分析和监控。它观察真实的 workload 模式，并输出快照，但不会假设直接执行 shadow 动作。

### SHADOW 模式

该模式用于仿真和兼容性评审。它适合在更高层系统中，将预期行为与 native 执行进行对照，而不直接在内核空间应用该行为。

这也是脚本中 `native_fallback=state.confidence_q15 == 0` 的意义：当 confidence 为 0 时，系统认为当前状态信任度很低，会回退到更安全、或更接近原生执行的策略。

---

## 6. 流程图

```mermaid
flowchart TD
    A[JSONL observation file] --> B[parse CLI args]
    B --> C[load_observations]
    C --> D[Observation objects]
    D --> E[extract_features]
    E --> F[StateMachine.update]
    F --> G[Derived workload state]
    G --> H{history exists?}
    H -- Yes --> I[observe_transition(prev_state, current_state)]
    H -- No --> J[skip transition learning]
    I --> K[generate prediction]
    J --> K
    K --> L{method}
    L -- second_order_markov --> M[predict_markov]
    L -- rule_trend --> N[predict_rule_trend]
    M --> O[make_snapshot]
    N --> O
    O --> P[write features.jsonl]
    O --> Q[write predictions.jsonl]
    Q --> R[last prediction line]
    R --> S[write_snapshot]
    R --> T[write_hint]
    S --> U[final prediction_snapshot.json]
    T --> V[final parp_shadow_hint.json]

    style A fill:#e8f5e9,stroke:#2e7d32
    style F fill:#e3f2fd,stroke:#1565c0
    style O fill:#fff3e0,stroke:#ef6c00
    style U fill:#fce4ec,stroke:#c2185b
    style V fill:#fce4ec,stroke:#c2185b
```

---

## 7. 总结

这个原型是一个轻量的用户态 workload 预测管线。它会把原始观测数据转换成特征，识别当前运行时状态，学习状态转移规律，预测下一阶段的 workload 模式，并生成适合校验、审计和 shadow 兼容性评审的快照产物。

它的设计故意保持简洁、数据驱动，并且与内核执行解耦，因此可以作为 PARP 仓库中不影响现有代码的独立原型层进行研究和评估。
