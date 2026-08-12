# hwe-to-linux-6.17.13-parp 复现包

本包用于从初始源码树 `linux-hwe-6.17-parp` 复现当前最新源码树 `linux-6.17.13-parp`。

目标运行内核：`6.17.13-myks-l02`

## 目录说明

- `source/overlay/`：权威复现内容。包含所有相对初始树新增或修改的文件。
- `source/delete_paths.txt`：需要从初始树删除的旧文件/旧目录。
- `source/baseline-key/`：初始 HWE 树中关键源码快照，便于对照。
- `source/current-key/`：最新 6.17.13-parp 中关键源码快照，便于审阅。
- `patches/0001-key-parp-tier2-bin-review.patch`：二级水位线、bin 评分、memcg 接口等关键路径的可读补丁。
- `patches/0000-full-tree-diff.patch.gz`：完整源码树文本 diff 压缩包，主要用于审查；精确复现以 overlay 为准。
- `scripts/apply_overlay.sh`：把初始源码树转换为最新源码树的应用脚本。
- `scripts/build_install_verify.sh`：使用本包配置构建内核。
- `scripts/verify_runtime.sh`：重启后验证运行态接口。
- `configs/current.config`：当前已验证构建配置。
- `docs/复现手册.md`：中文复现流程。
- `docs/源码说明.md`：关键源码位置说明。

## 快速复现

```bash
cd /path/to/this/bundle
bash scripts/apply_overlay.sh /path/to/linux-hwe-6.17-parp
bash scripts/build_install_verify.sh /path/to/linux-hwe-6.17-parp /path/to/build-linux-6.17.13-parp
sudo make -C /path/to/linux-hwe-6.17-parp O=/path/to/build-linux-6.17.13-parp modules_install install
sudo reboot
bash scripts/verify_runtime.sh
```

注意：`apply_overlay.sh` 会按 `source/delete_paths.txt` 删除初始 HWE 树中最新树不再保留的文件/目录。建议先复制一份初始源码树再执行。
