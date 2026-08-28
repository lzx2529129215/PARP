# Workload-Aware 设计

## 状态模型

状态由四个独立维度组成：`AccessOrder`、`ReuseMode`、`HotspotMode`、`PhaseMode`，并额外输出 `dominant`。UNKNOWN 表示证据不足，MIXED 表示多个压力或热点信号同时存在。

## 观测原则

优先消费区域窗口、DAMON 聚合结果和 cgroup/global delta。区域窗口不是逐页访问日志；缺少有序 region 事件时不推断顺序、循环或随机访问。

## 识别

第一版使用规则和状态机：进入/退出通过相同置信度阈值、最小驻留窗口和冷却窗口稳定切换。后续可把阈值分离为进入阈值与退出阈值，但不得绕过 UNKNOWN/MIXED 回退。

## 预测

提供规则趋势和二阶 Markov 两个基线。预测 horizon 默认 3000 ms，TTL 默认 5000 ms；预测器不调用 App-LSTM，也不以应用名称替代 Workload。

## 策略提示

SHADOW 只生成保护热点、保留下一阶段工作集、延后预清洗、压缩和迁移观察提示；Native 仍是实际策略。过期、序号异常、UNKNOWN 或非法 Q15 均回退 Native。
