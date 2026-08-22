# r11 内核构建目录说明

## 目录身份

本目录是以下内核的构建输出目录：

```text
6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
```

构建配置中的关键选项：

```text
CONFIG_LOCALVERSION="-parp-effective-tier-apply-r11-hashlock-lzx"
CONFIG_PARP_EFFECTIVE_TIER=y
CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY=y
```

## 移动记录

本目录原路径：

```text
/home/wency/PARP/lzx/kernel/v4-parp/work/build-linux-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx
```

现路径：

```text
/home/wency/build-linux-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx
```

移动日期：2026-08-21（America/Los_Angeles）。目录采用同一文件系统内的整体移动，
没有重新编译，也没有修改构建产物内容。

## 对应源码

该构建目录的源码树仍位于：

```text
/home/wency/PARP/lzx/kernel/v4-parp/work/src-linux-6.17.13-parp-effective-tier-apply-r9-lzx
```

源码目录名称虽然包含 `apply-r9`，但 r11 构建目录的 `source` 链接指向该工作树，
且工作树中包含 r11 的 256 个哈希自旋锁修改。主要修改文件为：

```text
mm/parp/core/effective_tier.c
include/linux/parp.h
mm/parp/tests/parp_test.c
```

这些 r10/r11 改动当前在 Git 中属于未提交的工作树修改。不要仅根据旧目录名或 Git
HEAD 判断其版本，也不要在未备份的情况下清理该源码工作树。

## 已安装产物

对应安装文件：

```text
/boot/vmlinuz-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
/boot/initrd.img-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
/boot/System.map-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
/boot/config-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+
/lib/modules/6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+/
```

`/lib/modules/6.17.13-parp-effective-tier-apply-r11-hashlock-lzx+/build` 应指向本目录。

## 使用注意事项

- 这是约 5.8 GiB 的构建输出，不是独立完整源码副本。
- 不要删除 `source` 指向的源码工作树。
- 后续增量构建应显式指定源码和输出目录，例如：

```bash
make -C /home/wency/PARP/lzx/kernel/v4-parp/work/src-linux-6.17.13-parp-effective-tier-apply-r9-lzx \
  O=/home/wency/build-linux-6.17.13-parp-effective-tier-apply-r11-hashlock-lzx
```

- 当前运行内核在移动时为 `6.17.13-parp-effective-tier-shadow-r9-lzx+`，并非 r11。
- 移动构建目录不会切换运行内核，也不会修改 GRUB 默认项。
