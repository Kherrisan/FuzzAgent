#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-}"

if [[ -t 1 && -z "${NO_COLOR+x}" && "${TERM:-}" != "dumb" ]]; then
    BLUE=$'\033[38;5;117m'
    RED=$'\033[38;5;210m'
    RESET=$'\033[0m'
else
    BLUE=''
    RED=''
    RESET=''
fi

color_path() {
    printf '%s%s%s' "$BLUE" "$1" "$RESET"
}

color_command() {
    printf '%s%s%s' "$RED" "$1" "$RESET"
}

# A new launch without --output gets its own directory. The stamp is local
# time to the minute. A second launch in that same minute gets -2, -3, ...
default_output_dir() {
    local stamp base candidate n
    stamp="$(date +%Y%m%d-%H%M)"
    base="$ROOT_DIR/output/${TARGET}-${stamp}"
    candidate="$base"
    n=2
    while [[ -e "$candidate" ]]; do
        candidate="${base}-${n}"
        n=$((n + 1))
    done
    printf '%s\n' "$candidate"
}

print_usage() {
    printf 'Usage: %s launch <source-directory-or-git-url> [options]\n       %s resume <output-directory> [options]\n       %s tui --output <directory> [hc-tui arguments...]\n       %s tui <container-name> [hc-tui arguments...]\n\n' "$0" "$0" "$0" "$0"
    cat <<'EOF'

Options for launch and resume (values follow each option):
  --duration DURATION       Campaign time budget (default: 24h)
  --api-base-url URL        OpenAI-compatible endpoint (default: https://api.openai.com/v1)
  --api-key KEY             API key (default: empty)
  --model MODEL             Legacy runtime model (default: empty)
  --github-token TOKEN      GitHub search token (default: empty)
  --auth-dir DIR            Host CLI auth bundle (default: .cache/auth in repository)
  --temperature NUMBER      LLM temperature (default: 0.6)
  --top-p NUMBER            LLM top-p (default: 0.9)
  --top-k NUMBER            LLM top-k (default: 20)
  --reply-timeout SECONDS   LLM reply timeout (default: 600)
  --max-agent-epochs COUNT  Agent epoch limit (default: 100)
  --reminder-token-ratio N  Reminder token ratio (default: 0.8)
  --max-token-len COUNT     Maximum token length (default: 128000)
  --oss-access-key-id KEY   OSS access key ID (default: empty)
  --oss-access-key-secret K OSS access key secret (default: empty)
  --oss-endpoint URL        OSS endpoint (default: empty)
  --oss-bucket NAME         OSS bucket (default: empty)
  --http-proxy URL          HTTP proxy (default: empty)
  --https-proxy URL         HTTPS proxy (default: empty)
  --no-proxy HOSTS          Proxy exclusions (default: localhost,127.0.0.1)

Options for launch only:
  --output DIR              Output directory (default: output/<target>-YYYYMMDD-HHMM)
  --engine hc|legacy        Runtime (default: hc)
  --config PATH             Campaign config inside the container (default: config/campaign.yaml)
  --session NAME            hc session (default: fz-<target>)
EOF
    printf '\n  %s\n  %s\n  %s\n' "$(color_command "$0 launch ./my-library --duration 10m --output ./results")" "$(color_command "$0 resume ./results")" "$(color_command "$0 tui --output ./results")"
}

if [[ "$ACTION" == "--help" || "$ACTION" == "-h" ]]; then
    print_usage
    exit 0
fi

if [[ "$ACTION" == tui ]]; then
    if [[ $# -lt 2 ]]; then print_usage >&2; exit 2; fi
    shift
    if [[ "$1" == --output ]]; then
        if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing directory for --output\n' >&2; exit 2; fi
        metadata="$2/.campaign/launcher.json"
        if [[ ! -f "$metadata" ]]; then printf 'Output directory is missing .campaign/launcher.json: %s\n' "$2" >&2; exit 2; fi
        if ! container="$(python3 - "$metadata" <<'PY'
import json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    name = data["container"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise ValueError("invalid container name")
    print(name)
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f"Invalid launcher metadata: {exc}", file=sys.stderr)
    sys.exit(1)
PY
        )"; then exit 1; fi
        shift 2
    else
        container="$1"
        shift
    fi
    if [[ "${1:-}" == -- ]]; then shift; fi
    if docker info >/dev/null 2>&1; then
        docker_cmd=(docker)
    elif sudo -n docker info >/dev/null 2>&1; then
        docker_cmd=(sudo -n docker)
    else
        printf 'Docker is unavailable or access was denied.\n' >&2; exit 1
    fi
    exec "${docker_cmd[@]}" exec -it -e "TERM=${TERM:-xterm-256color}" "$container" sh -c 'exec hc-tui --session "$HC_SESSION" "$@"' sh "$@"
fi
if [[ ( "$ACTION" != launch && "$ACTION" != resume ) || $# -lt 2 ]]; then
    print_usage >&2
    exit 2
fi
SOURCE="$2"
shift 2
OUTPUT_ARG=""; DURATION_ARG=24h; ENGINE_ARG=hc; CONFIG_ARG=config/campaign.yaml; SESSION_ARG=""
AUTH_DIR_ARG="$ROOT_DIR/.cache/auth"; API_BASE_ARG=https://api.openai.com/v1; API_KEY_ARG=""; MODEL_ARG=""; GITHUB_ARG=""
TEMPERATURE_ARG=0.6; TOP_P_ARG=0.9; TOP_K_ARG=20; TIMEOUT_ARG=600; EPOCHS_ARG=100; REMINDER_ARG=0.8; MAX_TOKEN_ARG=128000
OSS_ID_ARG=""; OSS_SECRET_ARG=""; OSS_ENDPOINT_ARG=""; OSS_BUCKET_ARG=""; HTTP_PROXY_ARG=""; HTTPS_PROXY_ARG=""; NO_PROXY_ARG=localhost,127.0.0.1
while [[ $# -gt 0 ]]; do
    option="$1"
    if [[ $# -lt 2 || -z "$2" ]]; then printf 'Missing value for %s\n' "$option" >&2; exit 2; fi
    case "$option" in
        --output|--engine|--config|--session)
            if [[ "$ACTION" != launch ]]; then printf '%s is only valid for launch\n' "$option" >&2; exit 2; fi
            case "$option" in
                --output) OUTPUT_ARG="$2" ;; --engine) ENGINE_ARG="$2" ;;
                --config) CONFIG_ARG="$2" ;; --session) SESSION_ARG="$2" ;;
            esac ;;
        --duration) DURATION_ARG="$2" ;; --api-base-url) API_BASE_ARG="$2" ;;
        --api-key) API_KEY_ARG="$2" ;; --model) MODEL_ARG="$2" ;;
        --github-token) GITHUB_ARG="$2" ;; --auth-dir) AUTH_DIR_ARG="$2" ;;
        --temperature) TEMPERATURE_ARG="$2" ;; --top-p) TOP_P_ARG="$2" ;;
        --top-k) TOP_K_ARG="$2" ;; --reply-timeout) TIMEOUT_ARG="$2" ;;
        --max-agent-epochs) EPOCHS_ARG="$2" ;; --reminder-token-ratio) REMINDER_ARG="$2" ;;
        --max-token-len) MAX_TOKEN_ARG="$2" ;; --oss-access-key-id) OSS_ID_ARG="$2" ;;
        --oss-access-key-secret) OSS_SECRET_ARG="$2" ;; --oss-endpoint) OSS_ENDPOINT_ARG="$2" ;;
        --oss-bucket) OSS_BUCKET_ARG="$2" ;; --http-proxy) HTTP_PROXY_ARG="$2" ;;
        --https-proxy) HTTPS_PROXY_ARG="$2" ;; --no-proxy) NO_PROXY_ARG="$2" ;;
        *) printf 'Unknown option: %s\n' "$option" >&2; exit 2 ;;
    esac
    shift 2
done
if [[ "$ENGINE_ARG" != hc && "$ENGINE_ARG" != legacy ]]; then
    printf 'Invalid engine: %s (expected hc or legacy)\n' "$ENGINE_ARG" >&2; exit 2
fi
export http_proxy="$HTTP_PROXY_ARG" HTTP_PROXY="$HTTP_PROXY_ARG" https_proxy="$HTTPS_PROXY_ARG" HTTPS_PROXY="$HTTPS_PROXY_ARG" no_proxy="$NO_PROXY_ARG" NO_PROXY="$NO_PROXY_ARG"

if [[ "$ACTION" == launch ]]; then
    if [[ -d "$SOURCE" ]]; then
        SOURCE_KIND=local
        SOURCE="$(cd "$SOURCE" && pwd -P)"
    elif [[ "$SOURCE" =~ ^(https?://|ssh://|git://|[^/@:]+@[^/:]+:) ]]; then
        SOURCE_KIND=remote
    else
        printf 'Source must be an existing directory or a Git repository URL: %s\n' "$SOURCE" >&2
        exit 2
    fi
    SOURCE_NAME="${SOURCE%/}"
    SOURCE_NAME="${SOURCE_NAME##*/}"
    SOURCE_NAME="${SOURCE_NAME##*:}"
    SOURCE_NAME="${SOURCE_NAME%.git}"
    TARGET="$(printf '%s' "$SOURCE_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g; s/^[^a-z0-9]*//; s/[^a-z0-9]*$//')"
    if [[ -z "$TARGET" ]]; then
        printf 'Could not derive a valid target name from: %s\n' "$SOURCE" >&2
        exit 2
    fi
else
    if [[ ! -d "$SOURCE" || ! -f "$SOURCE/.campaign/launcher.json" ]]; then
        printf 'Output directory is missing .campaign/launcher.json: %s\n' "$SOURCE" >&2
        exit 2
    fi
    RAW_OUTPUT_DIR="$(cd "$SOURCE" && pwd -P)"
    mapfile -t saved < <(python3 - "$RAW_OUTPUT_DIR/.campaign/launcher.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
for key in ('target', 'source', 'engine', 'config', 'session'):
    value = data[key]
    if not isinstance(value, str) or not value or '\n' in value:
        raise ValueError(f'invalid {key} in launcher metadata')
    print(value)
container = data['container']
if not isinstance(container, str) or not container or '\n' in container:
    raise ValueError('invalid container in launcher metadata')
print(container)
PY
    )
    if [[ ${#saved[@]} -ne 6 ]]; then
        printf 'Invalid campaign launcher metadata in %s\n' "$RAW_OUTPUT_DIR" >&2
        exit 1
    fi
    TARGET="${saved[0]}"
    TARGET_DIR="$RAW_OUTPUT_DIR/.campaign/source"
    if [[ "${saved[2]}" != hc ]]; then
        printf 'Resume is supported for hc campaigns only.\n' >&2
        exit 1
    fi
    if [[ ! -d "$TARGET_DIR" ]]; then
        printf 'Saved working source is missing: %s\n' "$TARGET_DIR" >&2
        exit 1
    fi
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
if ! "${docker_cmd[@]}" image inspect fuzzagent >/dev/null 2>&1; then
    printf 'The local fuzzagent image was not found. Build it first with:\n  %sdocker buildx build -t fuzzagent -f %s%s %s\n' \
        "$RED" "$(color_path "$ROOT_DIR/docker/Dockerfile")" "$RED" "$(color_path "$ROOT_DIR")" >&2
    exit 1
fi

if [[ "$ACTION" == launch && "$ENGINE_ARG" == legacy && ( -z "$API_KEY_ARG" || -z "$MODEL_ARG" ) ]]; then
    printf 'The legacy engine requires --api-key and --model.\n' >&2
    exit 1
fi

if [[ "$ACTION" == launch ]]; then
    mkdir -p "$ROOT_DIR/.cache/targets"
    if [[ "$SOURCE_KIND" == local ]]; then
        TARGET_DIR="$SOURCE"
        printf 'Using local source directory: %s\n' "$(color_path "$TARGET_DIR")"
    else
        TARGET_DIR="$ROOT_DIR/.cache/targets/$TARGET"
        if [[ -d "$TARGET_DIR/.git" ]]; then
            if ! git -C "$TARGET_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
                printf 'The cached checkout for %s is incomplete: %s\nRemove that directory and rerun this command.\n' \
                    "$TARGET" "$(color_path "$TARGET_DIR")" >&2
                exit 1
            fi
            CACHED_ORIGIN="$(git -C "$TARGET_DIR" remote get-url origin 2>/dev/null || true)"
            if [[ "$CACHED_ORIGIN" != "$SOURCE" ]]; then
                printf 'Cached checkout %s belongs to a different origin (%s). Remove or rename it before retrying.\n' "$(color_path "$TARGET_DIR")" "$CACHED_ORIGIN" >&2
                exit 1
            fi
            printf 'Using cached source checkout: %s\n' "$(color_path "$TARGET_DIR")"
        else
            if [[ -e "$TARGET_DIR" ]]; then
                printf 'Target path exists but is not a Git checkout: %s\nRemove or rename it, then retry.\n' \
                    "$(color_path "$TARGET_DIR")" >&2
                exit 1
            fi
            printf 'Cloning %s source into the local cache (this may take a while)...\n' "$TARGET"
            CLONE_TMP_DIR="$(mktemp -d "${TARGET_DIR}.clone.XXXXXX")"
            cleanup_clone() {
                if [[ -n "${CLONE_TMP_DIR:-}" && -d "$CLONE_TMP_DIR" ]]; then
                    rm -rf -- "$CLONE_TMP_DIR"
                fi
            }
            trap cleanup_clone EXIT
            if ! git clone --depth 1 --progress -- "$SOURCE" "$CLONE_TMP_DIR"; then
                printf 'Could not clone %s. Check network access and retry; incomplete clone data will be cleaned up.\n' "$TARGET" >&2
                exit 1
            fi
            mv "$CLONE_TMP_DIR" "$TARGET_DIR"
            CLONE_TMP_DIR=""
            trap - EXIT
            printf 'Source checkout ready: %s\n' "$(color_path "$TARGET_DIR")"
        fi
    fi
    if [[ -n "$OUTPUT_ARG" ]]; then
        RAW_OUTPUT_DIR="$(realpath -m -- "$OUTPUT_ARG")"
    else
        RAW_OUTPUT_DIR="$(realpath -m -- "$(default_output_dir)")"
    fi
    if [[ "$RAW_OUTPUT_DIR" == "$TARGET_DIR" || "$RAW_OUTPUT_DIR" == "$TARGET_DIR/"* ]]; then
        printf 'Output directory must be outside the source directory: %s\n' "$RAW_OUTPUT_DIR" >&2
        exit 1
    fi
    if [[ -d "$RAW_OUTPUT_DIR" && -n "$(ls -A "$RAW_OUTPUT_DIR")" ]]; then
        printf 'Output directory is not empty: %s. Use resume for an existing campaign.\n' "$RAW_OUTPUT_DIR" >&2
        exit 1
    fi
    mkdir -p "$RAW_OUTPUT_DIR/.campaign"
fi

if [[ "$ACTION" == resume ]]; then
    export CONTAINER_NAME="${saved[5]}"
else
    export CONTAINER_NAME="fuzzagent-$TARGET"
fi
export FUZZING_DURATION="$DURATION_ARG"
export COMMAND='bash /root/FuzzAgent/benchmark/run_benchmark.sh'
export WRITE_MODE=rw
export FUZZING_ENGINE=afl++
export FUZZAGENT_AUTO_CLEAN=0
export FUZZAGENT_DIR="$ROOT_DIR"
export SRC_DIR="$TARGET_DIR"
export REPO_BASE_NAME="$TARGET"
export RAW_OUTPUT_DIR
export FUZZAGENT_AUTH_DIR="$(realpath -m -- "$AUTH_DIR_ARG")" HOST_AUTH_DIR=/run/fuzzagent-auth
export OPENAI_BASE_URL="$API_BASE_ARG" OPENAI_API_KEY="$API_KEY_ARG" OPENAI_MODEL="$MODEL_ARG" GITHUB_TOKEN="$GITHUB_ARG"
export LLM_TEMPERATURE="$TEMPERATURE_ARG" LLM_TOP_P="$TOP_P_ARG" LLM_TOP_K="$TOP_K_ARG" LLM_REPLY_TIMEOUT="$TIMEOUT_ARG"
export MAX_AGENT_EPOCHS="$EPOCHS_ARG" REMINDER_TOKEN_RATIO="$REMINDER_ARG" MAX_TOKEN_LEN="$MAX_TOKEN_ARG"
export OSS_ACCESS_KEY_ID="$OSS_ID_ARG" OSS_ACCESS_KEY_SECRET="$OSS_SECRET_ARG" OSS_ENDPOINT="$OSS_ENDPOINT_ARG" OSS_BUCKET="$OSS_BUCKET_ARG"
export http_proxy="$HTTP_PROXY_ARG" HTTP_PROXY="$HTTP_PROXY_ARG" https_proxy="$HTTPS_PROXY_ARG" HTTPS_PROXY="$HTTPS_PROXY_ARG" no_proxy="$NO_PROXY_ARG" NO_PROXY="$NO_PROXY_ARG"
if [[ "$ACTION" == resume ]]; then
    export FUZZAGENT_ENGINE="${saved[2]}" FUZZAGENT_CONFIG="${saved[3]}" HC_SESSION="${saved[4]}"
else
    export FUZZAGENT_ENGINE="$ENGINE_ARG" FUZZAGENT_CONFIG="$CONFIG_ARG" HC_SESSION="${SESSION_ARG:-fz-$TARGET}"
    if [[ "$FUZZAGENT_ENGINE" == legacy ]]; then
        export FUZZAGENT_AUTO_CLEAN=1
    fi
    python3 - "$RAW_OUTPUT_DIR/.campaign/launcher.json" "$TARGET" "$TARGET_DIR" "$FUZZAGENT_ENGINE" "$FUZZAGENT_CONFIG" "$HC_SESSION" "$CONTAINER_NAME" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump(dict(zip(('target', 'source', 'engine', 'config', 'session', 'container'), sys.argv[2:])), f, indent=2)
PY
fi
export FUZZAGENT_RESUME="$([[ "$ACTION" == resume ]] && printf 1 || printf 0)"

printf '\nStarting FuzzAgent campaign\n'
printf '  Target:   %s\n' "$TARGET"
printf '  Duration: %s\n' "$FUZZING_DURATION"
printf '  Source:   %s\n' "$(color_path "$TARGET_DIR")"
printf '  Results:  %s\n' "$(color_path "$RAW_OUTPUT_DIR")"
printf '  Container: %s\n\n' "$CONTAINER_NAME"

mount_repo="$FUZZAGENT_DIR"
mount_output="$RAW_OUTPUT_DIR"
mount_src="$SRC_DIR"
mount_auth="$FUZZAGENT_AUTH_DIR"
if declare -F host_mount_path >/dev/null 2>&1; then
    mount_repo="$(host_mount_path "$mount_repo")"
    mount_output="$(host_mount_path "$mount_output")"
    mount_src="$(host_mount_path "$mount_src")"
    mount_auth="$(host_mount_path "$mount_auth")"
fi
if ! "${docker_cmd[@]}" run -d \
    --name "$CONTAINER_NAME" \
    --init \
    --tty \
    --interactive \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --cap-add SYS_ADMIN \
    --cap-add NET_ADMIN \
    --restart no \
    -v "${mount_repo}:/root/FuzzAgent:${WRITE_MODE}" \
    -v "${mount_output}:/root/FuzzAgent/output/${REPO_BASE_NAME}" \
    -v "${mount_src}:/src/${REPO_BASE_NAME}" \
    -v "${mount_auth}:/run/fuzzagent-auth:ro" \
    -e "FUZZAGENT_AUTO_CLEAN=${FUZZAGENT_AUTO_CLEAN:-0}" \
    -e "FUZZAGENT_RESUME=${FUZZAGENT_RESUME:-0}" \
    -e "HC_SESSION=${HC_SESSION:-fz-${REPO_BASE_NAME}}" \
    -e "FUZZAGENT_ENGINE=${FUZZAGENT_ENGINE:-hc}" \
    -e "FUZZAGENT_CONFIG=${FUZZAGENT_CONFIG:-config/campaign.yaml}" \
    -e "HOST_AUTH_DIR=${HOST_AUTH_DIR:-/run/fuzzagent-auth}" \
    -e "REPO_BASE_NAME=${REPO_BASE_NAME}" \
    -e "SRC=/root/src/${REPO_BASE_NAME}" \
    -e "OUTPUT=/root/FuzzAgent/output/${REPO_BASE_NAME}" \
    -e AFL_SKIP_CPUFREQ=1 \
    -e "FUZZING_ENGINE=${FUZZING_ENGINE}" \
    -e "GITHUB_TOKEN=${GITHUB_TOKEN}" \
    -e "OPENAI_BASE_URL=${OPENAI_BASE_URL}" \
    -e "OPENAI_API_KEY=${OPENAI_API_KEY}" \
    -e "OPENAI_MODEL=${OPENAI_MODEL}" \
    -e "LLM_TEMPERATURE=${LLM_TEMPERATURE:-0.6}" \
    -e "LLM_TOP_P=${LLM_TOP_P:-0.9}" \
    -e "LLM_TOP_K=${LLM_TOP_K:-20}" \
    -e "LLM_REPLY_TIMEOUT=${LLM_REPLY_TIMEOUT:-600}" \
    -e "MAX_AGENT_EPOCHS=${MAX_AGENT_EPOCHS:-100}" \
    -e "FUZZING_DURATION=${FUZZING_DURATION:-24h}" \
    -e "REMINDER_TOKEN_RATIO=${REMINDER_TOKEN_RATIO:-0.8}" \
    -e "MAX_TOKEN_LEN=${MAX_TOKEN_LEN:-128000}" \
    -e "OSS_ACCESS_KEY_ID=${OSS_ACCESS_KEY_ID:-}" \
    -e "OSS_ACCESS_KEY_SECRET=${OSS_ACCESS_KEY_SECRET:-}" \
    -e "OSS_ENDPOINT=${OSS_ENDPOINT:-}" \
    -e "OSS_BUCKET=${OSS_BUCKET:-}" \
    -e "http_proxy=${http_proxy:-}" \
    -e "https_proxy=${https_proxy:-}" \
    -e "HTTP_PROXY=${HTTP_PROXY:-}" \
    -e "HTTPS_PROXY=${HTTPS_PROXY:-}" \
    -e "no_proxy=${no_proxy:-localhost,127.0.0.1,211.65.197.92}" \
    -e "NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,211.65.197.92}" \
    fuzzagent \
    /bin/bash -c "$COMMAND"; then
    printf '\nCould not start the benchmark container. Check the Docker output above.\n' >&2
    exit 1
fi

CONTAINER_STATUS="$("${docker_cmd[@]}" inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)"
if [[ "$CONTAINER_STATUS" != "running" ]]; then
    printf '\nThe container did not stay running (status: %s). Recent logs:\n' "${CONTAINER_STATUS:-unknown}" >&2
    "${docker_cmd[@]}" logs --tail 40 "$CONTAINER_NAME" 2>&1 || true
    printf 'For more details, run: %s\n' "$(color_command "docker logs $CONTAINER_NAME")" >&2
    exit 1
fi

cat <<EOF

FuzzAgent is running in the background. This command can now be closed safely.

Execution logs are continuously saved to: $(color_path "$RAW_OUTPUT_DIR/.cache/logs")

Stop the benchmark early:
  $(color_command "docker rm -f $CONTAINER_NAME")


Attach the terminal UI:
  $(color_command "$0 tui --output '$RAW_OUTPUT_DIR'")
EOF
