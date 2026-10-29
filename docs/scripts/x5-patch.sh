#!/usr/bin/env bash
# Send working-tree changes relative to HEAD and apply them to the remote project.
# Includes tracked changes and non-ignored new files, without changing the real index.
# Required: X5_HOST, X5_USER, X5_PROJECT_DIR (absolute remote project directory).
# Optional: X5_SSH_PORT=22, X5_SSH_KEY (otherwise use SSH agent/default keys).
# The remote patch is kept at a generated temporary path for manual rollback.
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

AUTH_OPTS=(
    -o BatchMode=yes -o PreferredAuthentications=publickey
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=yes
    -o ConnectTimeout=10
)
if [[ -n "${X5_SSH_KEY:-}" ]]; then
    [[ -f "$X5_SSH_KEY" ]] || { echo 'X5_SSH_KEY is not a file' >&2; exit 2; }
    AUTH_OPTS+=(-i "$X5_SSH_KEY" -o IdentitiesOnly=yes)
fi
shell_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TEMP_DIR="$(mktemp -d -t x5-patch.XXXXXX)"
LOCAL_PATCH="$TEMP_DIR/changes.patch"
TEMP_INDEX="$TEMP_DIR/index"
cleanup() {
    rm -f -- "$LOCAL_PATCH" "$TEMP_INDEX" "$TEMP_INDEX.lock"
    rmdir -- "$TEMP_DIR"
}
trap cleanup EXIT

# A separate index keeps staged files and untracked status in the real repo intact.
GIT_INDEX_FILE="$TEMP_INDEX" git -C "$PROJECT_ROOT" read-tree HEAD
GIT_INDEX_FILE="$TEMP_INDEX" git -C "$PROJECT_ROOT" add -A -- .
GIT_INDEX_FILE="$TEMP_INDEX" git -C "$PROJECT_ROOT" diff --cached --binary HEAD > "$LOCAL_PATCH"
if [[ ! -s "$LOCAL_PATCH" ]]; then
    echo '[x5-patch] No working-tree changes relative to HEAD.'
    exit 0
fi

REMOTE_PATCH="$(ssh "${AUTH_OPTS[@]}" -p "$X5_SSH_PORT" -- "$X5_USER@$X5_HOST" 'mktemp /tmp/x5-patch.XXXXXXXX.patch')"
[[ "$REMOTE_PATCH" =~ ^/tmp/x5-patch\.[a-zA-Z0-9]+\.patch$ ]] || {
    echo '[x5-patch] Unexpected remote temporary path' >&2; exit 1;
}
SCP_HOST="$X5_HOST"
[[ "$SCP_HOST" != *:* ]] || SCP_HOST="[$SCP_HOST]"
scp "${AUTH_OPTS[@]}" -P "$X5_SSH_PORT" -- "$LOCAL_PATCH" "$X5_USER@$SCP_HOST:$REMOTE_PATCH"
REMOTE_CMD="cd -- $(shell_quote "$X5_PROJECT_DIR") && git apply --check -- $(shell_quote "$REMOTE_PATCH") && git apply -- $(shell_quote "$REMOTE_PATCH")"
ssh "${AUTH_OPTS[@]}" -p "$X5_SSH_PORT" -- "$X5_USER@$X5_HOST" "bash -lc $(shell_quote "$REMOTE_CMD")"
printf '[x5-patch] Applied successfully; remote patch retained at %s\n' "$REMOTE_PATCH"
