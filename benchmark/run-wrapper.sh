#!/usr/bin/env bash
# Run benchmark/run.sh, but give Docker the host path for each bind mount.
# This environment sees the tree at /home/user. The daemon sees that same
# tree under /data plus the volume path from findmnt.
set -euo pipefail

LOCAL_ROOT=/home/user

host_root() {
    if [[ -n "${FUZZAGENT_HOST_ROOT:-}" ]]; then
        printf '%s\n' "$FUZZAGENT_HOST_ROOT"
        return
    fi
    local source bracket
    source="$(findmnt -n -o SOURCE -T "$LOCAL_ROOT")"
    if [[ "$source" =~ \[([^]]+)\] ]]; then
        bracket="${BASH_REMATCH[1]}"
        if [[ "$bracket" != /* ]]; then
            bracket="/$bracket"
        fi
        printf '/data%s\n' "$bracket"
        return
    fi
    printf 'Cannot derive the host mount of %s from findmnt source: %s\n' "$LOCAL_ROOT" "$source" >&2
    return 1
}

host_mount_path() {
    local path="$1" root
    root="$(host_root)"
    if [[ "$path" == "$LOCAL_ROOT" || "$path" == "$LOCAL_ROOT"/* ]]; then
        printf '%s\n' "${root}${path#"$LOCAL_ROOT"}"
    else
        printf '%s\n' "$path"
    fi
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LOCAL_ROOT
export -f host_root host_mount_path
exec "$ROOT_DIR/benchmark/run.sh" "$@"
