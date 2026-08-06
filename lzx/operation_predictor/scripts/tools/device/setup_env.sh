# setup_env.sh — 设置 hdc + clang 环境变量，在脚本中 source 即可
# 用法: source scripts/device/setup_env.sh

# hdc (HarmonyOS Device Connector)
if [ -f "$HOME/Library/OpenHarmony/Sdk/23/toolchains/hdc" ]; then
    export PATH="$HOME/Library/OpenHarmony/Sdk/23/toolchains:$PATH"
elif [ -f "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc" ]; then
    export PATH="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
fi

# DevEco Studio SDK (for clang cross-compiler)
if [ -d "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native" ]; then
    export OHOS_SDK="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native"
fi

echo "[env] hdc=$(which hdc 2>/dev/null || echo 'NOT FOUND') clang=${OHOS_SDK:-NOT SET}" >&2
