#!/usr/bin/env bash
set -euo pipefail

# Official pages are used as the source of truth. Direct package URLs change,
# so callers may pass QQ_DEB_URL and WPS_DEB_URL after selecting x86_64 builds.
QQ_PAGE="https://im.qq.com/linuxqq/download.html"
WPS_PAGE="https://linux.wps.cn/"
download_dir="${XDG_CACHE_HOME:-$HOME/.cache}/memsched-exp/packages"
mkdir -p "$download_dir"

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "This first-round installer currently requires x86_64; detected $(uname -m)." >&2
  echo "Choose the matching architecture from $QQ_PAGE and $WPS_PAGE." >&2
  exit 2
fi

sudo apt-get update
sudo apt-get install -y \
  bpftrace bpftool clang llvm linux-tools-common \
  python3 python3-venv jq sysstat stress-ng fio \
  wmctrl xdotool shellcheck curl ca-certificates procps psmisc

resolve_deb_url() {
  local page="$1"
  curl -fsSL "$page" \
    | grep -Eo 'https?://[^"'"'"' <>]+(amd64|x86_64)[^"'"'"' <>]*\.deb([^"'"'"' <>]*)?' \
    | head -n 1
}

qq_url="${QQ_DEB_URL:-$(resolve_deb_url "$QQ_PAGE" || true)}"
wps_url="${WPS_DEB_URL:-$(resolve_deb_url "$WPS_PAGE" || true)}"

if [[ -z "$qq_url" || -z "$wps_url" ]]; then
  cat >&2 <<EOF
The official pages did not expose stable direct links in static HTML.
Open these pages, copy the x86_64 DEB links, then rerun:
  QQ:  $QQ_PAGE
  WPS: $WPS_PAGE

QQ_DEB_URL='https://...amd64.deb' WPS_DEB_URL='https://...x86_64.deb' $0
EOF
  exit 3
fi

curl -fL --retry 3 "$qq_url" -o "$download_dir/linuxqq_amd64.deb"
curl -fL --retry 3 "$wps_url" -o "$download_dir/wps_x86_64.deb"
sudo apt-get install -y "$download_dir/linuxqq_amd64.deb" "$download_dir/wps_x86_64.deb"

command -v qq
command -v wps
echo "QQ and WPS installed from their official download pages."
