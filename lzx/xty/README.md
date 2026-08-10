# PARP 复现包

这个目录把当前 VM 内核改动整理成可迁移的复现包，目标是让另一台机器可以按同样的源码、补丁、配置和验证步骤复现当前功能。

## 目录

- `patches/`：3 个源码补丁和 1 个合并补丁
- `source/current/`：当前版本源码快照
- `source/baseline/`：对应的改动前备份
- `configs/`：当前构建配置和内核 release
- `scripts/`：应用补丁、构建安装、运行验证、校验清单
- `docs/`：复现手册、交接说明、参考资料
- `manifest.json`：文件哈希和运行态摘要

## 快速开始

1. 先准备原始 GitHub 版 `v4-parp` 源码树。
2. 应用 `patches/series.txt` 里的补丁。
3. 拷贝 `configs/current.config` 作为构建配置。
4. 执行 `bash scripts/build_install_verify.sh`。
5. 重启后执行 `bash scripts/verify_runtime.sh`。

## 当前结论

- `mm/vmscan.c` 已把应用预测概率和 memcg 水位线接到 `shrink_many()` 的 memcg/bin 选择中。
- `mm/memcontrol-v1.c` 已补齐 `tier2_*` 的 cgroup v1 接口。
- `kernel/trace/trace_events.c` 已修正 trace event 参数位图处理，避免大参数列表下的位移风险。
