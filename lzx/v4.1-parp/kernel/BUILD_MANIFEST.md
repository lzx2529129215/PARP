# v4.1-PARP kernel build manifest

This directory contains a Linux 6.17.13 source tree with the v4-parp patch
applied and an out-of-tree build used for Test2 sink validation.

## Source and patches

- Upstream source: Linux 6.17.13 from kernel.org
- Tarball SHA256: `116802dc3ad1646163cc6ffe9bddba24a8069b569135ec0523cd799064f2edb9`
- Base PARP patch: `../v4-parp/patches/parp-v4-full.patch`
- v4.1 observability patch: `patches/v4.1-snapshot-observability.patch`
- Source tree: `src/linux-6.17.13-v4.1-parp/`
- Build tree: `build/linux-6.17.13-v4.1-parp/`

## Build configuration

The build uses `config-6.17.13-mglru` as the starting configuration and
enables the following required options:

```text
CONFIG_PARP=y
CONFIG_DEBUG_FS=y
CONFIG_MEMCG=y
CONFIG_LRU_GEN=y
CONFIG_LRU_GEN_ENABLED=y
```

The distro certificate paths are empty for this local experiment build.

## Build result

```text
kernelrelease: 6.17.13-v4.1-parp
target: make O=... -j2 bzImage
artifact: build/linux-6.17.13-v4.1-parp/arch/x86/boot/bzImage
debug image: build/linux-6.17.13-v4.1-parp/vmlinux
```

The kernel has not been installed into the host bootloader and the host has
not been rebooted.

## Test2 observability ABI

The v4-parp write interfaces remain:

```text
/sys/kernel/debug/parp/mode
/sys/kernel/debug/parp/app_bind
/sys/kernel/debug/parp/app_prior
```

The v4.1-only read interfaces are:

```text
/sys/kernel/debug/parp/snapshot
/sys/kernel/debug/parp/stats
```

`snapshot` exposes `version`, `created_ns`, `expires_ns`, `nr_priors`, and
`nr_bindings`. Each successful `app_bind` or `app_prior` update publishes a
new snapshot and increments `version`, allowing the runtime monitor to prove
the user-space write reached the kernel snapshot.
