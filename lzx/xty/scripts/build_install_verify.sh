#!/usr/bin/env bash
set -euo pipefail

SRC_DIR=${1:-/home/xty/v4-parp/work/linux-6.17.13-parp}
BUILD_DIR=${2:-/home/xty/v4-parp/work/build-linux-6.17.13-parp}
JOBS=${JOBS:-$(nproc)}

make -C "$SRC_DIR" O="$BUILD_DIR" olddefconfig
make -C "$SRC_DIR" O="$BUILD_DIR" -j"$JOBS" bzImage modules
KREL=$(make -s -C "$SRC_DIR" O="$BUILD_DIR" kernelrelease)

sudo make -C "$SRC_DIR" O="$BUILD_DIR" modules_install
sudo make -C "$SRC_DIR" O="$BUILD_DIR" install

if [ -e "/boot/initrd.img-$KREL" ]; then
  sudo update-initramfs -u -k "$KREL"
else
  sudo update-initramfs -c -k "$KREL"
fi

sudo update-grub
