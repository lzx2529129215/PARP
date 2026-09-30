# 指标定义

| 类别 | 字段 | 单位 | 来源 | 性质与限制 |
|---|---|---:|---|---|
| strict WSS | `real_wss_total_bytes` | bytes | clear 成功 T0 PID 的 `smaps:Referenced` | 严格窗口 proxy，非直接 PTE 真值 |
| strict WSS | `real_wss_anon/file/shmem/other/unknown_bytes` | bytes | smaps + VMA 分类 | Anon/File 是近似分类；mixed File+COW 可为 Unknown |
| legacy WSS | `wss_referenced_total_bytes` | bytes | T0∪T1 的 post-window smaps | 可纳入 T1-only PID |
| RSS | `rss_total/anon/file/shmem_bytes` | bytes | `/proc/<pid>/status` | 进程和可能重复计共享页 |
| PageFault | `page_fault_total_count` | count | user page-fault tracepoint | 用户态 exception，不等于 proc minflt+majflt |
| PageFault | `page_fault_anon/file/wp/shmem_handler_count` | count | kprobe handler path | 非精确 page type 分区 |
| cgroup | `cgroup_memory_*` | bytes | `memory.current` / `memory.stat` | cgroup accounting |
| PSI | `target_psi_*` / `system_psi_*` | % / us | memory pressure files | 时间窗口统计 |
| quality | `real_wss_ordering_ok` 等 | bool/count | collector metadata | 决定窗口是否可用 |

`*_4k_equivalent_bytes` 为 PageFault count 与运行时 base page size 的乘积，是派生比较单位。
