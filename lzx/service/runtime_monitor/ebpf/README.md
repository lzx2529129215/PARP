# eBPF 预留目录

Runtime Monitor v0 默认不启用 eBPF，也不修改内核。

全系统进程创建/执行/退出已经通过内核原生 proc connector
(`NETLINK_CONNECTOR/CN_IDX_PROC`) 实现，不需要加载 eBPF 程序。root helper 代码在
`helpers/proc_connector_helper.py`，用户态凭据校验客户端在
`collectors/process_events.py`。eBPF 目录仍只为未来的 path/syscall 级文件事件保留。

当前文件事件的无 eBPF fallback 已实现：

- 通过 `/proc/<pid>/fd` 轮询近似生成 `openat` 文件事件；
- 通过 `/proc/<pid>/maps` 轮询近似生成 `mmap` 文件事件；
- 通过 `/proc/<pid>/io` 或 cgroup `io.stat` 采集应用级 read/write 字节 delta；
- 无法可靠拿到 path 级 `read/write/fsync/rename`，这些需要后续 eBPF 或 tracefs 补充。
