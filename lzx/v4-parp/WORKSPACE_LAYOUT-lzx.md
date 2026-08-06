# PARP workspace layout

The active PARP project root is lzx/v4-parp. Its Linux source and build trees
remain local experiment dependencies; they are intentionally excluded from the
outer myself-kswapd repository.

## Versioned dependencies

- patches/parp-v4-full.patch is the existing base PARP patch.
- patches/linux-6.17.13-parp-effective-tier-series-lzx/ is the ordered,
  123-patch effective-tier series.
- configs/, docs/, reference/, and scripts/ carry project configuration and
  documentation.
- ../automation supplies the WPS, Files, and QQ automation dependency.
- ../runtime_monitor supplies runtime observation and the PARP bridge.

The workspace_layout-lzx.py tool under the active live-shadow worktree locates
these sibling dependencies from any path below v4-parp. It does not start an
experiment or change system state.

## Reproducible kernel source

The patch series starts from Linux commit
6609c4d49ebe220a5c40d3105c3f0e68f569ba1a. The current live-shadow source tip
is b4ee269927c3036af579415bb7efdacbb52ff126.

From a clean checkout at the recorded Linux commit, apply the series in lexical
order with git am --3way
patches/linux-6.17.13-parp-effective-tier-series-lzx/*.patch. The outer
repository deliberately does not track upstream/, work/, build/, or outputs/;
keeping them local avoids committing source mirrors, compiled objects, installed
artifacts, and experimental data.

## Active worktree

Use work/linux-6.17.13-parp-effective-tier-live-shadow for new PARP
effective-tier source work. The frozen
work/linux-6.17.13-parp-effective-tier worktree is retained for comparison
only.
