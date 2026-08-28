# 验收报告

## 当前状态

本阶段是隔离的用户态 OBSERVE/SHADOW 原型，不声明降低了真实 PageFault、Refault、PSI、Direct Reclaim 或前台尾延迟，也没有执行 APPLY。

## 已覆盖

- 多维状态结构和 UNKNOWN/MIXED 语义；
- 区域顺序存在/缺失时的保守处理；
- Q15 边界；
- 状态机最小驻留；
- 规则趋势和二阶 Markov 接口；
- snapshot 序列、TTL、WSS 和 Native fallback；
- PARP 风格适配提示的 apply=false 不变量。

## 后续验收

1. 用真实 region_windows.jsonl 和 cgroup delta 做离线回放。
2. 分别报告识别覆盖率、UNKNOWN/MIXED 比例、状态切换抖动和预测 hit@k/MRR。
3. 对齐五秒观察尾窗，比较 Native 与 SHADOW 的 headroom、refault、PSI、direct reclaim 和前台 p95/p99 延迟。
4. 只有真实 SHADOW 数据、ABI 和锁上下文审查全部通过，才讨论单独授权的 APPLY 补丁；本原型不自动进入该阶段。
