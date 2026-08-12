# v4.1-PARP kernel build manifest

v4.1 现在与 v4 共用一套 Linux 6.17.13 源码和一套增量构建输出。
本目录只保留可移植补丁和清单，不再拥有 `src/` 或 `build/` 副本。

## 唯一源码和构建输出

- 源码：`../../v4-parp/src/linux-6.17.13-parp-lzx/`
- 分支：`feat/parp-effective-tier-live-shadow`
- 当前 HEAD：`54812ffd8e0fc0acbf02c7df051333827f7d1caa`
- 构建输出：`../../v4-parp/build/effective-tier-live-shadow-r9-lzx/`
- 构建入口：`../../v4-parp/scripts/build-kernel-lzx.sh`
- kernel release：`6.17.13-parp-effective-tier-shadow-657a65b0a-r9+`

无参数运行构建脚本时增量构建 `bzImage`；需要其他目标时直接传入：

```bash
../../v4-parp/scripts/build-kernel-lzx.sh -j4 bzImage
../../v4-parp/scripts/build-kernel-lzx.sh kernelrelease
```

## 构建配置

保留的 r9 `.config` 启用：

```text
CONFIG_PARP=y
CONFIG_DEBUG_FS=y
CONFIG_MEMCG=y
CONFIG_LRU_GEN=y
CONFIG_LRU_GEN_ENABLED=y
CONFIG_PARP_EFFECTIVE_TIER=y
```

该内核未由这个清单自动安装到主机引导器，也不会自动重启。

## v4.1 观测 ABI

v4-parp 写入接口保持不变：

```text
/sys/kernel/debug/parp/mode
/sys/kernel/debug/parp/app_bind
/sys/kernel/debug/parp/app_prior
```

v4.1 只读接口已合入共用源码：

```text
/sys/kernel/debug/parp/snapshot
/sys/kernel/debug/parp/stats
```

`snapshot` 暴露 `version`、`created_ns`、`expires_ns`、`nr_priors` 和
`nr_bindings`。每次成功的 `app_bind` 或 `app_prior` 更新都会发布新快照。
`patches/v4.1-snapshot-observability.patch` 仅用于向其他基线移植这两个观测接口。
