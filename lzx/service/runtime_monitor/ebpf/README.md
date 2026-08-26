# eBPF 预留目录

Runtime Monitor v0 默认不启用 eBPF，也不修改内核。

当前无 eBPF fallback 已实现：

- 通过 `/proc/<pid>/fd` 轮询近似生成 `openat` 文件事件；
- 通过 `/proc/<pid>/maps` 轮询近似生成 `mmap` 文件事件；
- 通过 `/proc/<pid>/io` 或 cgroup `io.stat` 采集应用级 read/write 字节 delta；
- 无法可靠拿到 path 级 `read/write/fsync/rename`，这些需要后续 eBPF 或 tracefs 补充。

