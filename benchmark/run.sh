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

print_usage() {
    printf 'Usage: %s launch <source-directory-or-git-url> [--output <directory>]\n       %s resume <output-directory>\n       %s tui <container-name> [hc-tui arguments...]\n\n' "$0" "$0" "$0"
    cat <<'EOF'

Start a fuzzing campaign for a local source directory or Git repository URL.
The default fuzzing duration is 24h; override it with FUZZING_DURATION, e.g.:
EOF
    printf '  %s\n  %s\n  %s\n' "$(color_command "FUZZING_DURATION=10m $0 launch ./my-library --output ./results")" "$(color_command "$0 resume ./results")" "$(color_command "$0 tui fuzzagent-my-library")"
}

if [[ "$ACTION" == "--help" || "$ACTION" == "-h" ]]; then
    print_usage
    exit 0
fi

if [[ "$ACTION" == tui ]]; then
    if [[ $# -lt 2 ]]; then print_usage >&2; exit 2; fi
    container="$2"
    shift 2
    exec docker exec -it -e "TERM=${TERM:-xterm-256color}" "$container" sh -c 'exec hc-tui --session "$HC_SESSION" "$@"' sh "$@"
fi
if [[ ( "$ACTION" != launch && "$ACTION" != resume ) || $# -lt 2 ]]; then
    print_usage >&2
    exit 2
fi
SOURCE="$2"
OUTPUT_ARG=""
if [[ "$ACTION" == launch && $# -eq 4 && "$3" == --output && -n "$4" ]]; then
    OUTPUT_ARG="$4"
elif [[ $# -ne 2 ]]; then
    print_usage >&2
    exit 2
fi

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
PY
    )
    if [[ ${#saved[@]} -ne 5 ]]; then
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
    printf 'Docker was not found. Install Docker Engine and Docker Compose, then retry.\n' >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    printf 'Docker is not available. Start Docker and make sure your user can access it, then retry.\n' >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    printf 'Docker Compose v2 is required. Install the Docker Compose plugin, then retry.\n' >&2
    exit 1
fi
if ! docker image inspect fuzzagent >/dev/null 2>&1; then
    printf 'The local fuzzagent image was not found. Build it first with:\n  %sdocker buildx build -t fuzzagent -f %s%s %s\n' \
        "$RED" "$(color_path "$ROOT_DIR/docker/Dockerfile")" "$RED" "$(color_path "$ROOT_DIR")" >&2
    exit 1
fi

ENV_FILE="$ROOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Missing %s. Create it from %s and configure the LLM endpoint:\n' \
        "$(color_path "$ENV_FILE")" "$(color_path "$ROOT_DIR/.env_template")" >&2
    printf '  %scp %s%s %s\n' "$RED" "$(color_path "$ROOT_DIR/.env_template")" "$RED" "$(color_path "$ENV_FILE")" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
missing_config=()
for name in OPENAI_BASE_URL OPENAI_API_KEY OPENAI_MODEL; do
    if [[ -z "${!name:-}" ]]; then
        missing_config+=("$name")
    fi
done
if (( ${#missing_config[@]} > 0 )); then
    printf 'Configure the following required values in %s: %s\n' "$(color_path "$ENV_FILE")" "${missing_config[*]}" >&2
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
    RAW_OUTPUT_DIR="$(realpath -m -- "${OUTPUT_ARG:-$ROOT_DIR/output/$TARGET}")"
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

export CONTAINER_NAME="fuzzagent-$TARGET"
export FUZZING_DURATION="${FUZZING_DURATION:-24h}"
export COMMAND='bash /root/FuzzAgent/benchmark/run_benchmark.sh'
export WRITE_MODE=rw
export FUZZING_ENGINE=afl++
export FUZZAGENT_AUTO_CLEAN=0
export FUZZAGENT_DIR="$ROOT_DIR"
export SRC_DIR="$TARGET_DIR"
export REPO_BASE_NAME="$TARGET"
export RAW_OUTPUT_DIR
if [[ "$ACTION" == resume ]]; then
    export FUZZAGENT_ENGINE="${saved[2]}" FUZZAGENT_CONFIG="${saved[3]}" HC_SESSION="${saved[4]}"
else
    export FUZZAGENT_ENGINE="${FUZZAGENT_ENGINE:-hc}" FUZZAGENT_CONFIG="${FUZZAGENT_CONFIG:-config/campaign.yaml}" HC_SESSION="${HC_SESSION:-fz-$TARGET}"
    if [[ "$FUZZAGENT_ENGINE" == legacy ]]; then
        export FUZZAGENT_AUTO_CLEAN=1
    fi
    python3 - "$RAW_OUTPUT_DIR/.campaign/launcher.json" "$TARGET" "$TARGET_DIR" "$FUZZAGENT_ENGINE" "$FUZZAGENT_CONFIG" "$HC_SESSION" <<'PY'
import json, sys
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump(dict(zip(('target', 'source', 'engine', 'config', 'session'), sys.argv[2:])), f, indent=2)
PY
fi
export FUZZAGENT_RESUME="$([[ "$ACTION" == resume ]] && printf 1 || printf 0)"

printf '\nStarting FuzzAgent campaign\n'
printf '  Target:   %s\n' "$TARGET"
printf '  Duration: %s\n' "$FUZZING_DURATION"
printf '  Source:   %s\n' "$(color_path "$TARGET_DIR")"
printf '  Results:  %s\n' "$(color_path "$RAW_OUTPUT_DIR")"
printf '  Container: %s\n\n' "$CONTAINER_NAME"

if ! docker compose -p "$CONTAINER_NAME" -f "$ROOT_DIR/docker_template.yml" up -d; then
    printf '\nCould not start the benchmark container. Check the Docker output above.\n' >&2
    exit 1
fi

CONTAINER_STATUS="$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)"
if [[ "$CONTAINER_STATUS" != "running" ]]; then
    printf '\nThe container did not stay running (status: %s). Recent logs:\n' "${CONTAINER_STATUS:-unknown}" >&2
    docker logs --tail 40 "$CONTAINER_NAME" 2>&1 || true
    printf 'For more details, run: %s\n' "$(color_command "docker logs $CONTAINER_NAME")" >&2
    exit 1
fi

cat <<EOF

FuzzAgent is running in the background. This command can now be closed safely.

Execution logs are continuously saved to: $(color_path "$RAW_OUTPUT_DIR/.cache/logs")

Stop the benchmark early:
  $(color_command "docker rm -f $CONTAINER_NAME")


Attach the terminal UI:
  $(color_command "$0 tui $CONTAINER_NAME")
EOF
