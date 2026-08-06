#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  automation/run_rich_scenario.sh --list
  automation/run_rich_scenario.sh SCENARIO [run_automation options]

SCENARIO:
  qq      QQ 登录、会话搜索、浏览和安全草稿（不发送）
  files   Files 导航、搜索、视图切换和隔离目录操作
  wps     WPS Writer、Spreadsheet、Presentation 三组件办公流
  cross   Files -> WPS -> QQ 跨应用联动（QQ 不发送）
  thunderbird  Thunderbird 邮件搜索、导航、地址簿与日历
  vlc          VLC 播放、进度、音量、全屏与播放列表
  gimp         GIMP 图像浏览、缩放、界面切换与 XCF 保存
  keepassxc    KeePassXC 安全 UI 场景（不读取真实密码库）
  libreoffice  LibreOffice Writer、Calc、Impress 三组件办公流
  five-app     上述五应用的两轮切换和前台验证

Examples:
  automation/run_rich_scenario.sh --list
  automation/run_rich_scenario.sh files --dry-run
  automation/run_rich_scenario.sh qq --var QQ_TEST_CONTACT="我的电脑"
  automation/run_rich_scenario.sh cross --dry-run
  automation/run_rich_scenario.sh five-app --dry-run
EOF
}

if (($# == 0)); then
  usage
  exit 2
fi

if [[ "$1" == "--list" || "$1" == "-l" ]]; then
  usage
  exit 0
fi

scenario_key="$1"
shift

case "$scenario_key" in
  qq) scenario="scenario_rich_qq_safe.json" ;;
  files) scenario="scenario_rich_files_workflow.json" ;;
  wps) scenario="scenario_rich_wps_office.json" ;;
  cross) scenario="scenario_rich_cross_app.json" ;;
  thunderbird) scenario="scenario_rich_thunderbird.json" ;;
  vlc) scenario="scenario_rich_vlc.json" ;;
  gimp) scenario="scenario_rich_gimp.json" ;;
  keepassxc) scenario="scenario_rich_keepassxc_safe.json" ;;
  libreoffice) scenario="scenario_rich_libreoffice.json" ;;
  five-app) scenario="scenario_rich_five_app_switch.json" ;;
  -h|--help) usage; exit 0 ;;
  *) echo "Unknown rich scenario: $scenario_key" >&2; usage >&2; exit 2 ;;
esac

exec "${SCRIPT_DIR}/run_automation.sh" \
  --scenario "${PROJECT_ROOT}/configs/automation/${scenario}" \
  --scenario-id "rich_${scenario_key}" \
  "$@"
