#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SCENARIO="${SCENARIO:-${PROJECT_ROOT}/configs/automation/scenario_local_wps.json}"
DISPLAY_ARG="${DISPLAY_ARG:-}"
XAUTHORITY_ARG="${XAUTHORITY_ARG:-}"
DRY_RUN=0
TRACE_OUTPUT="${TRACE_OUTPUT:-}"
SESSION_ID="${SESSION_ID:-}"
SCENARIO_ID="${SCENARIO_ID:-}"
TEST_SLICE="${TEST_SLICE:-huawei-test.slice}"
RESET_FILES=0
SCENARIO_VARS=()

usage() {
    cat <<EOF
Usage:
  $0 [--scenario FILE] [--display :0] [--xauthority PATH] [--trace-output PATH] [--session-id ID] [--scenario-id ID] [--test-slice SLICE] [--var NAME=VALUE] [--reset-files] [--dry-run]

Examples:
  cd "${PROJECT_ROOT}"
  ./run_automation.sh
  ./run_automation.sh --dry-run
  ./run_automation.sh --scenario configs/automation/scenario_local_wps_files.json --test-slice huawei-test.slice
  ./run_automation.sh --scenario configs/automation/wps_perf_0040_word.json --var WPS_WORD_FILE="\$HOME/samples/word.docx" --var WPS_IMAGE_FILE="\$HOME/samples/image.png"
EOF
}

while (($# > 0)); do
    case "$1" in
        --scenario)
            SCENARIO="$2"
            shift 2
            ;;
        --display)
            DISPLAY_ARG="$2"
            shift 2
            ;;
        --xauthority)
            XAUTHORITY_ARG="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --trace-output)
            TRACE_OUTPUT="$2"
            shift 2
            ;;
        --session-id)
            SESSION_ID="$2"
            shift 2
            ;;
        --scenario-id)
            SCENARIO_ID="$2"
            shift 2
            ;;
        --test-slice)
            TEST_SLICE="$2"
            shift 2
            ;;
        --reset-files)
            RESET_FILES=1
            shift
            ;;
        --var)
            SCENARIO_VARS+=("$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ "$SCENARIO" != /* && ! -f "$SCENARIO" && -f "${PROJECT_ROOT}/${SCENARIO}" ]]; then
    SCENARIO="${PROJECT_ROOT}/${SCENARIO}"
elif [[ "$SCENARIO" != /* && ! -f "$SCENARIO" && -f "${SCRIPT_DIR}/${SCENARIO}" ]]; then
    SCENARIO="${SCRIPT_DIR}/${SCENARIO}"
fi
if [[ -z "$SESSION_ID" ]]; then
    SESSION_ID="$(date +%Y%m%d_%H%M%S)"
fi
if [[ -z "$SCENARIO_ID" ]]; then
    base="$(basename -- "$SCENARIO")"
    SCENARIO_ID="${base%.json}"
fi
if [[ -z "$TRACE_OUTPUT" ]]; then
    TRACE_OUTPUT="${PROJECT_ROOT}/outputs/runtime_monitor/${SESSION_ID}/automation_trace.csv"
fi
mkdir -p "$(dirname -- "$TRACE_OUTPUT")"

# ===========================================================================
# Environment setup for GUI applications (especially Snap Firefox)
# Snap Firefox + systemd-run --scope needs HOME, WAYLAND, DBus, PATH, etc.
# ===========================================================================

# --- core user identity ---
export HOME="${HOME:?HOME is not set; run this script as the logged-in Ubuntu desktop user}"
export USER="${USER:-$(id -un)}"
export LOGNAME="${LOGNAME:-$USER}"

# --- runtime directory & D-Bus ---
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

# --- GUI backend defaults ---
# The current automation uses xdotool, so GTK apps should prefer X11/Xwayland.
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export MOZ_ENABLE_WAYLAND="${MOZ_ENABLE_WAYLAND:-1}"
export GDK_BACKEND="${GDK_BACKEND:-x11}"

# --- X11 / Xwayland detection (for xdotool, WPS, QQ) ---
# User-supplied --display takes precedence; otherwise auto-detect.
if [ -n "${DISPLAY_ARG:-}" ]; then
    export DISPLAY="$DISPLAY_ARG"
elif [ -z "${DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ]; then
    export DISPLAY=:0
fi

# --- XAUTHORITY detection ---
# User-supplied --xauthority takes precedence; otherwise auto-detect.
if [ -n "${XAUTHORITY_ARG:-}" ]; then
    export XAUTHORITY="$XAUTHORITY_ARG"
elif [ -z "${XAUTHORITY:-}" ]; then
    GDM_AUTH="${XDG_RUNTIME_DIR}/gdm/Xauthority"
    MUTTER_AUTH=$(ls -t "${XDG_RUNTIME_DIR}"/.mutter-Xwaylandauth.* 2>/dev/null | head -1 || true)
    if [ -f "$GDM_AUTH" ]; then
        export XAUTHORITY="$GDM_AUTH"
    elif [ -n "${MUTTER_AUTH:-}" ]; then
        export XAUTHORITY="$MUTTER_AUTH"
    elif [ -f "$HOME/.Xauthority" ]; then
        export XAUTHORITY="$HOME/.Xauthority"
    fi
fi

# --- ensure snap Firefox is on PATH ---
case ":$PATH:" in
    *:/snap/bin:*) ;;
    *) export PATH="$PATH:/snap/bin" ;;
esac

# ===========================================================================
# Ensure Firefox automation profile directory exists
# Snap Firefox cannot access host /tmp (private mount namespace).
# ===========================================================================
mkdir -p "$HOME/firefox_profiles/automation"
chown -R "$USER:$USER" "$HOME/firefox_profiles" 2>/dev/null || true

# ===========================================================================
# Experiment cgroup slice setup
# ===========================================================================
echo "[run_automation] Setting up slice: ${TEST_SLICE}"
systemctl --user set-property "${TEST_SLICE}" MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes 2>/dev/null || \
    echo "[run_automation] warning: could not set accounting on ${TEST_SLICE} (may already be active)"

# Optionally reset Nautilus (DBus singleton) before launching Files
if ((RESET_FILES == 1)); then
    echo "[run_automation] --reset-files: quitting existing Nautilus instances"
    nautilus --quit 2>/dev/null || true
    sleep 1
fi

# ===========================================================================
# Diagnostic output (always printed to help debugging)
# ===========================================================================
echo "=============================================="
echo "[run_automation] Environment diagnostics"
echo "=============================================="
echo "USER=$USER"
echo "HOME=$HOME"
echo "DISPLAY=$DISPLAY"
echo "XAUTHORITY=$XAUTHORITY"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}"
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
echo "MOZ_ENABLE_WAYLAND=$MOZ_ENABLE_WAYLAND"
echo "GDK_BACKEND=$GDK_BACKEND"
echo "PATH=$PATH"
echo "which firefox: $(which firefox 2>/dev/null || echo 'NOT FOUND')"
echo "firefox --version: $(firefox --version 2>/dev/null || echo 'FAILED')"
echo "SESSION_ID=$SESSION_ID"
echo "SCENARIO_ID=$SCENARIO_ID"
echo "TRACE_OUTPUT=$TRACE_OUTPUT"
echo "TEST_SLICE=$TEST_SLICE"
echo "RESET_FILES=$RESET_FILES"
echo "SCENARIO_VARS=${SCENARIO_VARS[*]:-}"
echo "=============================================="

# Build Python arguments (only pass --display/--xauthority if user explicitly set them)
ARGS=("$SCENARIO")
ARGS+=("--session-id" "$SESSION_ID" "--scenario-id" "$SCENARIO_ID")
if [ -n "${TRACE_OUTPUT:-}" ]; then
    ARGS+=("--trace-output" "$TRACE_OUTPUT")
fi
if [ -n "${DISPLAY_ARG:-}" ]; then
    ARGS+=("--display" "$DISPLAY_ARG")
fi
if [ -n "${XAUTHORITY_ARG:-}" ]; then
    ARGS+=("--xauthority" "$XAUTHORITY_ARG")
fi
if ((DRY_RUN == 1)); then
    ARGS+=("--dry-run")
fi
for item in "${SCENARIO_VARS[@]}"; do
    ARGS+=("--var" "$item")
done
ARGS+=("--test-slice" "$TEST_SLICE")

echo "[run_automation] scenario: $SCENARIO"
python3 "$SCRIPT_DIR/app_automation.py" "${ARGS[@]}"
