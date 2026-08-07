#!/usr/bin/env bash
set -euo pipefail

DEBUGFS_PATH="/sys/kernel/debug/lru_gen_workload_markov"
OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --debugfs-path)
      DEBUGFS_PATH="$2"
      shift 2
      ;;
    --output)
      OUTPUT="$2"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

if ! sudo -n test -e "$DEBUGFS_PATH"; then
  echo "FAIL: debugfs 不存在: $DEBUGFS_PATH" >&2
  exit 1
fi

write_command() {
  printf '%s\n' "$1" | sudo -n tee "$DEBUGFS_PATH" >/dev/null
}

read_debugfs() {
  sudo -n cat "$DEBUGFS_PATH"
}

SELF_CGROUP="$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)"
SELF_CGROUP_PATH="/sys/fs/cgroup${SELF_CGROUP}"
if [[ ! -d "$SELF_CGROUP_PATH" ]]; then
  echo "FAIL: 无法解析当前进程的真实 cgroup: $SELF_CGROUP_PATH" >&2
  exit 1
fi
SELF_CGROUP_ID="$(stat -c '%i' "$SELF_CGROUP_PATH")"

write_command "policy mode observe"
write_command "policy threshold 9000 5000 2000"
write_command "policy factor 700 750 1000 1100 1250"
write_command "policy bounds 700 1300 1 4096"
write_command "policy default 3000 1000 1000 0"
write_command "app bind 99 $SELF_CGROUP_ID 300000"
write_command "app probability 1 9500 300000"
write_command "app probability 2 7000 300000"
write_command "app probability 3 3500 300000"
write_command "app probability 4 1000 300000"

VALID_SNAPSHOT="$(read_debugfs)"
VALID_THRESHOLDS="$(awk '$1 == "policy_config" && $2 == "thresholds" {print $0}' <<<"$VALID_SNAPSHOT")"
VALID_FACTORS="$(awk '$1 == "policy_config" && $2 == "factors" {print $0}' <<<"$VALID_SNAPSHOT")"

if write_command "policy threshold 4000 5000 2000" 2>/dev/null; then
  echo "FAIL: 非法 threshold 顺序被内核接受" >&2
  exit 1
fi
if [[ "$(read_debugfs | awk '$1 == "policy_config" && $2 == "thresholds" {print $0}')" != "$VALID_THRESHOLDS" ]]; then
  echo "FAIL: 非法 threshold 导致配置被部分更新" >&2
  exit 1
fi

if write_command "policy factor 0 750 1000 1100 1250" 2>/dev/null; then
  echo "FAIL: 非法 factor 被内核接受" >&2
  exit 1
fi
if [[ "$(read_debugfs | awk '$1 == "policy_config" && $2 == "factors" {print $0}')" != "$VALID_FACTORS" ]]; then
  echo "FAIL: 非法 factor 导致配置被部分更新" >&2
  exit 1
fi

if write_command "policy mode invalid" 2>/dev/null; then
  echo "FAIL: 非法 mode 被内核接受" >&2
  exit 1
fi
write_command "policy mode observe"

if [[ -n "$OUTPUT" ]]; then
  mkdir -p "$(dirname "$OUTPUT")"
  sudo -n cat "$DEBUGFS_PATH" > "$OUTPUT"
  RESULT_FILE="$OUTPUT"
else
  RESULT_FILE="$(mktemp)"
  trap 'rm -f "$RESULT_FILE"' EXIT
  sudo -n cat "$DEBUGFS_PATH" > "$RESULT_FILE"
fi

grep -q '^policy_config mode observe$' "$RESULT_FILE"
grep -q '^policy_config thresholds 9000 5000 2000$' "$RESULT_FILE"
grep -q "^bind 99 $SELF_CGROUP_ID " "$RESULT_FILE"
grep -q '^prob 1 9500 ' "$RESULT_FILE"
grep -q '^prob 2 7000 ' "$RESULT_FILE"
grep -q '^prob 3 3500 ' "$RESULT_FILE"
grep -q '^prob 4 1000 ' "$RESULT_FILE"

echo "probability_is_rank_based=false"
echo "PASS: 策略参数、真实 cgroup bind、四组受控概率和非法参数原子性均验证通过。"
