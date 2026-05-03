#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_WAIT=10  # seconds to wait for process to exit

# Check if tmux session and window exist
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Error: tmux session '$TMUX_SESSION' does not exist"
    exit 1
fi

if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Error: window '$TMUX_WINDOW' not found in session '$TMUX_SESSION'"
    exit 1
fi

# Get the pane PID and check if uv run ccbot is running.
#
# Originally used `pstree -a $PANE_PID`, but `pstree` is GNU-only and not
# installed by default on macOS — the call silently returned nothing and
# `is_ccbot_running` was always false, so restart.sh skipped the kill step
# and `tmux send-keys` typed the start command into the still-running
# bot's stdin. Result: the user thought they restarted, but the live
# process was hours-old stale code. Use `pgrep` instead — descends from
# PANE_PID down through the process tree, no external pstree needed.
PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')

# Walk the pgrep -P parent-of tree starting at PANE_PID, returning all
# descendant PIDs (one per line). Pure POSIX-ish, works on macOS + Linux.
descendants_of() {
    local parent="$1"
    local children
    children=$(pgrep -P "$parent" 2>/dev/null || true)
    [ -z "$children" ] && return
    for c in $children; do
        echo "$c"
        descendants_of "$c"
    done
}

is_ccbot_running() {
    local pids
    pids=$(descendants_of "$PANE_PID")
    [ -z "$pids" ] && return 1
    # shellcheck disable=SC2086
    ps -o command= -p $pids 2>/dev/null \
        | grep -qE 'uv[[:space:]]+run[[:space:]]+ccbot|\.venv/bin/ccbot'
}

# Echo the uv parent PID for the running ccbot tree (used as the SIGTERM
# target so a hung Python process can be reaped cleanly).
ccbot_uv_pid() {
    local pids
    pids=$(descendants_of "$PANE_PID")
    [ -z "$pids" ] && return
    # shellcheck disable=SC2086
    ps -o pid=,command= -p $pids 2>/dev/null \
        | awk '/uv[[:space:]]+run[[:space:]]+ccbot/ { print $1; exit }'
}

# Stop existing process if running
if is_ccbot_running; then
    echo "Found running ccbot process, sending Ctrl-C..."
    tmux send-keys -t "$TARGET" C-c

    # Wait for process to exit
    waited=0
    while is_ccbot_running && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
    done

    if is_ccbot_running; then
        echo "Process did not exit after ${MAX_WAIT}s, sending SIGTERM..."
        UV_PID=$(ccbot_uv_pid)
        if [ -n "$UV_PID" ]; then
            kill "$UV_PID" 2>/dev/null || true
            sleep 2
        fi
        if is_ccbot_running; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$UV_PID" 2>/dev/null || true
            sleep 1
        fi
    fi

    echo "Process stopped."
else
    echo "No ccbot process running in $TARGET"
fi

# Brief pause to let the shell settle
sleep 1

# Start ccbot
echo "Starting ccbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run ccbot" Enter

# Verify startup and show logs
sleep 3
if is_ccbot_running; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
