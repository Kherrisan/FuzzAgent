#!/usr/bin/env bash
# Enter a running container and start hc-tui on its existing hc session.
# Keep HC_TUI_VERSION in sync with the pin in docker/Dockerfile.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HC_TUI_VERSION="${HC_TUI_VERSION:-0.1.2}"

usage() {
    cat <<EOF
Usage: bin/hc-tui.sh [container] [hc-tui args]

Enter a running container and start hc-tui on the hc session it is already
serving. When the container has no hc-tui binary yet, this installs release
v${HC_TUI_VERSION} into /usr/local/bin first.

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

if ! "${DOCKER[@]}" exec "$container" command -v hc-tui >/dev/null 2>&1; then
    printf 'Installing hc-tui %s into %s\n' "$HC_TUI_VERSION" "$container"
    "${DOCKER[@]}" exec \
        -e "HC_TUI_VERSION=${HC_TUI_VERSION}" \
        -e HC_TUI_INSTALL_DIR=/usr/local/bin \
        -e HC_TUI_SKIP_CORE=1 \
        "$container" \
        sh -c 'curl -fsSL https://followcat.github.io/hc-tui/install.sh | sh'
fi

"${DOCKER[@]}" cp "$ROOT_DIR/bin/hc-tui-attach" "$container:/usr/local/bin/hc-tui-attach"
"${DOCKER[@]}" exec "$container" chmod 755 /usr/local/bin/hc-tui-attach

if [[ ! -t 0 || ! -t 1 ]]; then
    printf 'hc-tui needs an interactive terminal. Run bin/hc-tui.sh from a shell.\n' >&2
    exit 1
fi

exec "${DOCKER[@]}" exec -it \
    -e "TERM=${TERM:-xterm-256color}" \
    "$container" \
    hc-tui-attach "${args[@]}"
