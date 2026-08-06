#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<'EOF'
Usage:
  automation/run_wps_case.sh CASE [run_automation options]

CASE:
  0010  稻壳模板浏览和下载
  0020  新建 Word、PPT、Excel 文档
  0030  连续打开 10 个文档并切换标签页
  0040  Word 文档综合操作
  0050  PPT 文档综合操作
  0060  Excel 文档综合操作
  0070  PDF 文档综合操作

Examples:
  automation/run_wps_case.sh 0010 --dry-run
  automation/run_wps_case.sh 0040 \
    --var WPS_WORD_FILE="$HOME/samples/word.docx" \
    --var WPS_IMAGE_FILE="$HOME/samples/image.png"
EOF
}

if (($# == 0)); then
    usage
    exit 2
fi

case_id="$1"
shift

case "$case_id" in
    0010) scenario="wps_perf_0010_templates.json" ;;
    0020) scenario="wps_perf_0020_new_documents.json" ;;
    0030) scenario="wps_perf_0030_ten_documents.json" ;;
    0040) scenario="wps_perf_0040_word.json" ;;
    0050) scenario="wps_perf_0050_presentation.json" ;;
    0060) scenario="wps_perf_0060_spreadsheet.json" ;;
    0070) scenario="wps_perf_0070_pdf.json" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown WPS case: $case_id" >&2; usage >&2; exit 2 ;;
esac

exec "$SCRIPT_DIR/run_automation.sh" \
    --scenario "$PROJECT_ROOT/configs/automation/$scenario" \
    --scenario-id "Perf_WPS_${case_id}" \
    "$@"
