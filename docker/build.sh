#!/usr/bin/env bash
# Build the fuzzagent image with the proxy settings of the current shell.
#
# Build steps run in a container on the Docker bridge network, where the host
# loopback is not reachable. Loopback addresses in the proxy URLs are therefore
# rewritten to the bridge gateway before they are passed as build args.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY="${FUZZAGENT_CONTAINER_GATEWAY:-172.17.0.1}"
TAG="fuzzagent"
GITHUB_TOKEN_ARG=""
NO_CACHE=()
PULL=()

usage() {
    cat <<'EOF'
Usage: docker/build.sh [options]

Options:
  --tag NAME        Image tag (default: fuzzagent)
  --gateway ADDR    Bridge gateway for loopback rewriting (default: 172.17.0.1)
  --github-token T  Pass a GitHub token to the build as a BuildKit secret
  --no-cache        Rebuild every layer
  --pull            Always pull the base image
  -h, --help        Show this help

Proxy values are read from http_proxy/https_proxy/no_proxy in the environment.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing value for --tag\n' >&2; exit 2; fi
            TAG="$2"; shift 2 ;;
        --gateway)
            if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing value for --gateway\n' >&2; exit 2; fi
            GATEWAY="$2"; shift 2 ;;
        --github-token)
            if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing value for --github-token\n' >&2; exit 2; fi
            GITHUB_TOKEN_ARG="$2"; shift 2 ;;
        --no-cache) NO_CACHE=(--no-cache); shift ;;
        --pull) PULL=(--pull); shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

rewrite_proxy() {
    local value="${1:-}"
    value="${value//127.0.0.1/$GATEWAY}"
    value="${value//localhost/$GATEWAY}"
    printf '%s' "$value"
}

HTTP_PROXY_VALUE="$(rewrite_proxy "${http_proxy:-${HTTP_PROXY:-}}")"
HTTPS_PROXY_VALUE="$(rewrite_proxy "${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}")"
NO_PROXY_VALUE="${no_proxy:-${NO_PROXY:-localhost,127.0.0.1}}"

build_args=()
if [[ -n "$HTTP_PROXY_VALUE" ]]; then
    build_args+=(--build-arg "http_proxy=$HTTP_PROXY_VALUE" --build-arg "HTTP_PROXY=$HTTP_PROXY_VALUE")
fi
if [[ -n "$HTTPS_PROXY_VALUE" ]]; then
    build_args+=(--build-arg "https_proxy=$HTTPS_PROXY_VALUE" --build-arg "HTTPS_PROXY=$HTTPS_PROXY_VALUE")
fi
if [[ -n "$NO_PROXY_VALUE" ]]; then
    build_args+=(--build-arg "no_proxy=$NO_PROXY_VALUE" --build-arg "NO_PROXY=$NO_PROXY_VALUE")
fi

if [[ -n "$GITHUB_TOKEN_ARG" ]]; then
    export GITHUB_TOKEN="$GITHUB_TOKEN_ARG"
fi
secret=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    secret=(--secret id=github_token,env=GITHUB_TOKEN)
fi

if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker was not found. Install Docker Engine, then retry.\n' >&2
    exit 1
fi
if docker info >/dev/null 2>&1; then
    docker_cmd=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
    docker_cmd=(sudo -n docker)
else
    printf 'Docker is unavailable or access was denied.\n' >&2
    exit 1
fi

printf 'Building %s from %s\n' "$TAG" "$ROOT_DIR/docker/Dockerfile"
if [[ -n "$HTTP_PROXY_VALUE" || -n "$HTTPS_PROXY_VALUE" ]]; then
    printf '  Proxy: http=%s https=%s no=%s\n' "${HTTP_PROXY_VALUE:-none}" "${HTTPS_PROXY_VALUE:-none}" "$NO_PROXY_VALUE"
fi

# The Dockerfile needs BuildKit for its secret mount, so prefer buildx and fall
# back to `docker build` with BuildKit enabled.
if "${docker_cmd[@]}" buildx version >/dev/null 2>&1; then
    exec "${docker_cmd[@]}" buildx build \
        --load \
        -t "$TAG" \
        -f "$ROOT_DIR/docker/Dockerfile" \
        "${build_args[@]}" "${secret[@]}" "${NO_CACHE[@]}" "${PULL[@]}" \
        "$ROOT_DIR"
fi

printf 'docker buildx was not found; retrying with DOCKER_BUILDKIT=1 docker build.\n' >&2
export DOCKER_BUILDKIT=1
exec "${docker_cmd[@]}" build \
    -t "$TAG" \
    -f "$ROOT_DIR/docker/Dockerfile" \
    "${build_args[@]}" "${secret[@]}" "${NO_CACHE[@]}" "${PULL[@]}" \
    "$ROOT_DIR"
