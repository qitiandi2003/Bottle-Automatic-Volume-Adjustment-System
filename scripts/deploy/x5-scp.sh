#!/usr/bin/env bash
# Copy files using key-based authentication; no HTTP or device commands are started.
# Required: X5_HOST, X5_USER, X5_PROJECT_DIR (absolute remote project directory).
# Optional: X5_SSH_PORT=22, X5_SSH_KEY (otherwise use SSH agent/default keys).
# Usage: x5-scp.sh push <local...> [--to <remote_path>]
#        x5-scp.sh pull <remote_path> <local_path>
# Relative remote paths are resolved under X5_PROJECT_DIR.
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

SCP_OPTS=(
    -o BatchMode=yes -o PreferredAuthentications=publickey
    -o PubkeyAuthentication=yes -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=yes
    -o ConnectTimeout=10 -P "$X5_SSH_PORT"
)
if [[ -n "${X5_SSH_KEY:-}" ]]; then
    [[ -f "$X5_SSH_KEY" ]] || { echo 'X5_SSH_KEY is not a file' >&2; exit 2; }
    SCP_OPTS+=(-i "$X5_SSH_KEY" -o IdentitiesOnly=yes)
fi
SCP_HOST="$X5_HOST"
[[ "$SCP_HOST" != *:* ]] || SCP_HOST="[$SCP_HOST]"
remote_path() {
    if [[ "$1" == /* ]]; then printf '%s' "$1"; else printf '%s/%s' "${X5_PROJECT_DIR%/}" "$1"; fi
}

mode="${1:-}"
case "$mode" in
    push)
        shift
        sources=()
        destination="${X5_PROJECT_DIR%/}/"
        while (( $# )); do
            if [[ "$1" == --to ]]; then
                (( $# == 2 )) || { echo 'Use --to <remote_path> after the source files' >&2; exit 2; }
                destination="$(remote_path "$2")"
                shift 2
            else
                # Prefix relative names so local colons and leading '-' are not SCP syntax.
                source_path="$1"
                [[ "$source_path" == /* ]] || source_path="./$source_path"
                sources+=("$source_path")
                shift
            fi
        done
        (( ${#sources[@]} )) || { echo 'Usage: x5-scp.sh push <local...> [--to <remote_path>]' >&2; exit 2; }
        exec scp "${SCP_OPTS[@]}" -- "${sources[@]}" "$X5_USER@$SCP_HOST:$destination"
        ;;
    pull)
        shift
        (( $# == 2 )) || { echo 'Usage: x5-scp.sh pull <remote_path> <local_path>' >&2; exit 2; }
        destination="$2"
        [[ "$destination" == /* ]] || destination="./$destination"
        exec scp "${SCP_OPTS[@]}" -- "$X5_USER@$SCP_HOST:$(remote_path "$1")" "$destination"
        ;;
    *)
        echo 'Usage: x5-scp.sh push <local...> [--to <remote_path>] | pull <remote_path> <local_path>' >&2
        exit 2
        ;;
esac
