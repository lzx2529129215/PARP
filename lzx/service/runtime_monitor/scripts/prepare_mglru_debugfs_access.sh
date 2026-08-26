#!/usr/bin/env bash
# prepare_mglru_debugfs_access.sh — ACL-first minimal permissions for MGLRU debugfs
#
# Prefers ACL (setfacl) when available:
#   setfacl -m u:${USER}:--x /sys/kernel/debug
#   setfacl -m u:${USER}:rw- /sys/kernel/debug/lru_gen_workload_markov
#
# Falls back to group-based when setfacl not available.
#
# Usage:
#   --check-only   Show current permissions and ACL, exit
#   --apply        Apply ACL or group changes (requires sudo)
#   --restore      Restore original state (requires sudo)

set -euo pipefail

DEBUGFS_DIR="/sys/kernel/debug"
DEBUGFS_FILE="${DEBUGFS_DIR}/lru_gen_workload_markov"
TARGET_USER="${TARGET_USER:-$(whoami)}"
TARGET_FILE_MODE="0660"

SAVE_DIR="${HOME}/.cache/mglru_debugfs_perms"
mkdir -p "$SAVE_DIR"
ACL_SAVE_DIR="${SAVE_DIR}/acl"
mkdir -p "$ACL_SAVE_DIR"
PERMISSION_MODE_FILE="$SAVE_DIR/permission_mode.txt"

HAS_ACL=false
if command -v setfacl &>/dev/null && command -v getfacl &>/dev/null; then
    HAS_ACL=true
fi

get_perm_mode() { echo "$HAS_ACL" && echo "ACL"; }

# ── helpers ──────────────────────────────────────────────────

can_rw_file() {
    [[ -r "$DEBUGFS_FILE" ]] && [[ -w "$DEBUGFS_FILE" ]] 2>/dev/null
}

check_only() {
    echo "=== Debugfs Access Check ==="
    echo "  User        : $(whoami)"
    echo "  Group       : $(id -gn)"
    echo "  ACL available: $HAS_ACL"
    echo ""
    if $HAS_ACL; then
        echo "  debugfs dir ACL:"
        sudo getfacl "$DEBUGFS_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (cannot read)"
        echo "  MGLRU file ACL:"
        sudo getfacl "$DEBUGFS_FILE" 2>/dev/null | sed 's/^/    /' || echo "    (cannot read)"
    else
        echo "  debugfs dir   : $(sudo stat -c '%a %U %G' "$DEBUGFS_DIR" 2>/dev/null || echo unknown)"
        echo "  MGLRU file    : $(sudo stat -c '%a %U %G' "$DEBUGFS_FILE" 2>/dev/null || echo unknown)"
    fi
    echo ""
    if can_rw_file; then
        echo "  RESULT: user '$(whoami)' CAN read/write ${DEBUGFS_FILE}"
        return 0
    else
        echo "  RESULT: user '$(whoami)' CANNOT read/write ${DEBUGFS_FILE}"
        return 1
    fi
}

apply() {
    echo "=== Applying debugfs access ($([ "$HAS_ACL" = true ] && echo ACL || echo GROUP_FALLBACK)) ==="

    # Save original state
    sudo getfacl "$DEBUGFS_DIR" > "$ACL_SAVE_DIR/debugfs_dir.acl" 2>/dev/null || true
    sudo getfacl "$DEBUGFS_FILE" > "$ACL_SAVE_DIR/debugfs_file.acl" 2>/dev/null || true
    sudo stat -c '%a %U %G' "$DEBUGFS_DIR" > "$SAVE_DIR/debugfs_dir_stat.txt" 2>/dev/null || true
    sudo stat -c '%a %U %G' "$DEBUGFS_FILE" > "$SAVE_DIR/debugfs_file_stat.txt" 2>/dev/null || true
    echo "  Saved original state to ${SAVE_DIR}/"

    if $HAS_ACL; then
        echo "  [ACL] Adding user ${TARGET_USER} traverse to debugfs dir..."
        set +e
        sudo setfacl -m "u:${TARGET_USER}:--x" "$DEBUGFS_DIR"
        dir_rc=$?
        echo "  [ACL] Adding user ${TARGET_USER} rw to MGLRU file..."
        sudo setfacl -m "u:${TARGET_USER}:rw-" "$DEBUGFS_FILE"
        file_rc=$?
        set -e
        if [[ "$dir_rc" -ne 0 || "$file_rc" -ne 0 ]]; then
            echo "  [ACL] setfacl failed (dir_rc=$dir_rc file_rc=$file_rc); using GROUP_FALLBACK"
            HAS_ACL=false
        fi
    fi
    if ! $HAS_ACL; then
        echo "  [GROUP] Setting dir mode 711..."
        sudo chmod 711 "$DEBUGFS_DIR"
        echo "  [GROUP] Setting file group to ${TARGET_USER}..."
        sudo chgrp "$TARGET_USER" "$DEBUGFS_FILE"
        echo "  [GROUP] Setting file mode ${TARGET_FILE_MODE}..."
        sudo chmod "$TARGET_FILE_MODE" "$DEBUGFS_FILE"
        printf '%s\n' "GROUP_FALLBACK" > "$PERMISSION_MODE_FILE"
    else
        printf '%s\n' "ACL" > "$PERMISSION_MODE_FILE"
    fi

    echo ""
    if can_rw_file; then
        echo "  RESULT: user '$(whoami)' CAN now read/write ${DEBUGFS_FILE}"
        return 0
    else
        echo "  RESULT: STILL cannot access."
        return 1
    fi
}

restore() {
    echo "=== Restoring debugfs access ==="
    saved_mode=""
    [[ -f "$PERMISSION_MODE_FILE" ]] && read -r saved_mode < "$PERMISSION_MODE_FILE"
    if [[ "$saved_mode" == "ACL" && -f "$ACL_SAVE_DIR/debugfs_dir.acl" ]]; then
        echo "  [ACL] Restoring dir ACL..."
        sudo setfacl --restore="$ACL_SAVE_DIR/debugfs_dir.acl" 2>/dev/null || true
        echo "  [ACL] Restoring file ACL..."
        sudo setfacl --restore="$ACL_SAVE_DIR/debugfs_file.acl" 2>/dev/null || true
        # Also remove any lingering user ACL if restore didn't fully clean
        sudo setfacl -x "u:${TARGET_USER}" "$DEBUGFS_DIR" 2>/dev/null || true
        sudo setfacl -x "u:${TARGET_USER}" "$DEBUGFS_FILE" 2>/dev/null || true
    else
        if [[ -f "$SAVE_DIR/debugfs_dir_stat.txt" ]]; then
            read -r mode owner group < "$SAVE_DIR/debugfs_dir_stat.txt"
            sudo chmod "$mode" "$DEBUGFS_DIR" 2>/dev/null || true
        fi
        if [[ -f "$SAVE_DIR/debugfs_file_stat.txt" ]]; then
            read -r mode owner group < "$SAVE_DIR/debugfs_file_stat.txt"
            sudo chmod "$mode" "$DEBUGFS_FILE" 2>/dev/null || true
            sudo chown "${owner}:${group}" "$DEBUGFS_FILE" 2>/dev/null || true
        fi
    fi
    rm -f "$PERMISSION_MODE_FILE"
    echo "  RESULT: Permissions restored."
}

# ── main ─────────────────────────────────────────────────────

case "${1:-}" in
    --check-only) check_only ;;
    --apply)      apply ;;
    --restore)    restore ;;
    *)
        echo "Usage: $0 {--check-only|--apply|--restore}"
        echo "  Mode: $([ "$HAS_ACL" = true ] && echo ACL || echo GROUP_FALLBACK)"
        exit 1 ;;
esac
