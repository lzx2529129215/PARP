# 架构

采集由三条独立链路组成，最终在同一窗口 summary 中汇合。

```text
target cgroup
 ├─ cgroup.procs → T0/T1 PID → clear_refs + smaps → strict/legacy WSS
 ├─ proc status/stat/statm → RSS、进程 fault 辅助值、CPU
 ├─ cgroup memory.* + PSI + meminfo → 内存与压力状态
 └─ eBPF tracepoint/kprobes → per-CPU PageFault counters → loader CSV
```

strict real WSS 仅使用操作前 clear 成功的 T0 PID；legacy WSS 使用 T0∪T1 可读 PID。eBPF map 不保存 WSS page bitmap，BPF 仅负责 PageFault 计数与 cgroup 过滤。
