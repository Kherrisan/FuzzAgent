#!/usr/bin/env bash
# Enter a running container and start hc-tui on its existing hc session.
set -euo pipefail

usage() {
    cat <<EOF
Usage: bin/hc-tui.sh [container] [hc-tui args]

Enter a running container and start hc-tui on the hc session it is already
serving. The image must already provide hc-tui and hc-tui-attach.

  bin/hc-tui.sh
  bin/hc-tui.sh fuzz-gnutls-stdio-3h
  bin/hc-tui.sh fuzz-gnutls -- --version

Set FUZZAGENT_CONTAINER to choose the container when you omit the name.
A name is required when more than one fuzzagent container is running.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker was not found. Install Docker Engine, then retry.\n' >&2
    exit 1
fi
if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
else
    printf 'Docker is not available. Start Docker and make sure your user can access it, then retry.\n' >&2
    exit 1
fi

container="${FUZZAGENT_CONTAINER:-}"
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --)
            shift
            args+=("$@")
            break
            ;;
        -*)
            args+=("$1")
            shift
            ;;
        *)
            if [[ -z "$container" ]]; then
                container=$1
            else
                args+=("$1")
            fi
            shift
            ;;
    esac
done

if [[ -z "$container" ]]; then
    mapfile -t running < <("${DOCKER[@]}" ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^fuzzagent(:|$)/ { print $1 }')
    if [[ ${#running[@]} -eq 1 ]]; then
        container=${running[0]}
    elif [[ ${#running[@]} -eq 0 ]]; then
        printf 'No running fuzzagent container. Pass its name: bin/hc-tui.sh <container>\n' >&2
        exit 1
    else
        printf 'Several fuzzagent containers are running. Pass one name:\n' >&2
        printf '  %s\n' "${running[@]}" >&2
        exit 1
    fi
fi

if ! "${DOCKER[@]}" inspect "$container" >/dev/null 2>&1; then
    printf 'Container %s was not found.\n' "$container" >&2
    exit 1
fi
state="$("${DOCKER[@]}" inspect -f '{{.State.Status}}' "$container")"
if [[ "$state" != "running" ]]; then
    printf 'Container %s is %s, not running.\n' "$container" "$state" >&2
    exit 1
fi

# `command` is a shell builtin. docker exec looks up a binary, so this has to
# run inside sh.
missing="$("${DOCKER[@]}" exec "$container" sh -c '
    missing=
    command -v hc-tui >/dev/null 2>&1 || missing="$missing hc-tui"
    command -v hc-tui-attach >/dev/null 2>&1 || missing="$missing hc-tui-attach"
    printf "%s" "$missing"
')"
if [[ -n "$missing" ]]; then
    printf 'Container %s is missing:%s\n' "$container" "$missing" >&2
    printf 'Rebuild the fuzzagent image. This script does not install them.\n' >&2
    exit 1
fi

if [[ ! -t 0 || ! -t 1 ]]; then
    printf 'hc-tui needs an interactive terminal. Run bin/hc-tui.sh from a shell.\n' >&2
    exit 1
fi

exec "${DOCKER[@]}" exec -it \
    -e "TERM=${TERM:-xterm-256color}" \
    "$container" \
    hc-tui-attach "${args[@]}"
