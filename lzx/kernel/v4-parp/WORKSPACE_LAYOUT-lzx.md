# PARP 单一源码工作区

`lzx/kernel/v4-parp` 现在只保留一套可修改的 Linux 源码和一套可复用的构建输出：

- 源码：`src/linux-6.17.13-parp-lzx/`
- 构建输出：`build/effective-tier-live-shadow-r9-lzx/`
- 统一构建入口：`scripts/build-kernel-lzx.sh`

源码目录是独立 Git 仓库，当前分支为
`feat/parp-effective-tier-live-shadow`，当前 HEAD 为
`54812ffd8e0fc0acbf02c7df051333827f7d1caa`。它不再是依附其他 clean tree
的 linked worktree。旧实验分支和提交对象仍保留在该仓库的 `.git`
中，但不再占用多套工作目录。

## 增量编译

直接运行：

```bash
cd /home/lzxxxxxx/桌面/huawei/myself-kswapd/lzx/kernel/v4-parp
./scripts/build-kernel-lzx.sh
```

无参数时默认增量构建 `bzImage`。也可以把任意 make 参数传给它，例如：

```bash
./scripts/build-kernel-lzx.sh -j4 bzImage
./scripts/build-kernel-lzx.sh kernelrelease
```

不要再创建新的 `O=` 目录；该脚本始终复用上面的 r9 输出。
现有 r9 的 Kbuild 文本依赖缓存已迁移到上述新路径，不需要兼容
符号链接或第二套源码。

## v4.1 集成

`../v4.1-parp` 保留用户态评估、配置、测试和可移植补丁，不再维护自己的
Linux `src/` 或 `build/`。v4.1 的 `snapshot`/`stats` 观测接口已合入这套源码。

## 可复现资料

- `patches/parp-v4-full.patch` 是原始 PARP 基础补丁。
- `patches/linux-6.17.13-parp-effective-tier-series-lzx/linux-6.17.13-parp-effective-tier-squashed-lzx.patch`
  是从上游基线到当前 HEAD 的单一总补丁；原 126 个细粒度提交仍保留在源码仓库 Git 历史中。
- `configs/`、`docs/`、`reference/` 和 `scripts/` 保留配置、设计和工具。
- Linux 上游基线提交为 `6609c4d49ebe220a5c40d3105c3f0e68f569ba1a`。
