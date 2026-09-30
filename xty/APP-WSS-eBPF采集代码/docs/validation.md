# 已验证范围

一次真实 WPS 18-operation 单轮验证使用 strict real-WSS collector 完成：

| 项目 | 结果 |
|---|---:|
| REP013 smoke | PASS |
| 18 操作 PASS/WARN/FAIL | 18 / 0 / 0 |
| strict real WSS valid | 18 / 18 |
| `clear_refs → operation → smaps` valid | 18 / 18 |
| T0 clear 未覆盖 PID | 0 |
| strict PID smaps read failure | 0 |
| automation failure | 0 |

REP001 与 REP006 出现 T1-only PID。legacy WSS 纳入 T0∪T1，strict real WSS 排除未在窗口前 clear 的 T1-only PID，因此两者存在明显差异。该结果验证机制和审计字段，不代表所有 Linux 应用或所有内核版本都已验证。
