#!/usr/bin/env bash
# Run a command in the remote project, or open an interactive shell.
# Required: X5_HOST, X5_USER, X5_PROJECT_DIR (absolute remote directory).
# Optional: X5_SSH_PORT=22, X5_SSH_KEY (otherwise use SSH agent/default keys).
# Examples: bash docs/scripts/x5.sh
#           bash docs/scripts/x5.sh 'python3 main.py'
#           bash docs/scripts/x5.sh python3 -c 'print(1 + 1)'
set -euo pipefail

: "${X5_HOST:?Set X5_HOST to the device hostname or IP address}"
: "${X5_USER:?Set X5_USER to the remote SSH user}"
: "${X5_PROJECT_DIR:?Set X5_PROJECT_DIR to the absolute remote project directory}"
X5_SSH_PORT="${X5_SSH_PORT:-22}"
[[ "$X5_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]*$ ]] || { echo 'Invalid X5_HOST' >&2; exit 2; }
[[ "$X5_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*$ ]] || { echo 'Invalid X5_USER' >&2; exit 2; }
[[ "$X5_PROJECT_DIR" == /* ]] || { echo 'X5_PROJECT_DIR must be absolute' >&2; exit 2; }
[[ "$X5_SSH_PORT" =~ ^[0-9]{1,5}$ ]] && [ "$X5_SSH_PORT" -ge 1 ] && [ "$X5_SSH_PORT" -le 65535 ] || {
    echo 'Invalid X5_SSH_PORT' >&2; exit 2;
}

SSH_OPTS=(
    -o BatchMode=yes -o PreferredAuthentications=publickey
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=yes
    -o ConnectTimeout=10 -p "$X5_SSH_PORT"
)
if [[ -n "${X5_SSH_KEY:-}" ]]; then
    [[ -f "$X5_SSH_KEY" ]] || { echo 'X5_SSH_KEY is not a file' >&2; exit 2; }
    SSH_OPTS+=(-i "$X5_SSH_KEY" -o IdentitiesOnly=yes)
fi

# POSIX shell quoting also protects directories/arguments containing apostrophes.
shell_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
REMOTE_CMD="cd -- $(shell_quote "$X5_PROJECT_DIR") && "
if (( $# == 0 )); then
    REMOTE_CMD+='exec bash -l'
    exec ssh "${SSH_OPTS[@]}" -t -- "$X5_USER@$X5_HOST" "bash -lc $(shell_quote "$REMOTE_CMD")"
elif (( $# == 1 )); then
    # One argument is an explicit shell command, including any requested pipelines.
    REMOTE_CMD+="$1"
else
    for arg in "$@"; do
        REMOTE_CMD+="$(shell_quote "$arg") "
    done
fi
exec ssh "${SSH_OPTS[@]}" -- "$X5_USER@$X5_HOST" "bash -lc $(shell_quote "$REMOTE_CMD")"
