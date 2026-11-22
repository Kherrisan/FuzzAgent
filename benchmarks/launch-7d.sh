#!/usr/bin/env bash
# Launch one 7-day campaign per benchmark target (codex gpt-6-luna, effort high).
#
# Each target gets its own container (fuzzagent-<derived>), hc session (fz-bNN)
# and output directory (output/7d-<id>-<stamp>). Launches are staggered so the
# containers do not all start their agents and apt work at the same second.
#
#   benchmarks/launch-7d.sh            # launch all 20
#   SMOKE_STAGGER=60 benchmarks/launch-7d.sh
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DURATION="${CAMPAIGN_DURATION:-7d}"
STAGGER="${SMOKE_STAGGER:-30}"
LOG_DIR="$ROOT_DIR/benchmarks/7d-launch"
MANIFEST="$LOG_DIR/manifest.tsv"
mkdir -p "$LOG_DIR"
[[ -f "$MANIFEST" ]] || printf 'id\tderived\tcontainer\tsession\toutput\tstatus\n' > "$MANIFEST"

TARGETS=(
  "cjson|https://github.com/DaveGamble/cJSON.git"
  "pugixml|https://github.com/zeux/pugixml.git"
  "zlib|https://github.com/madler/zlib.git"
  "sqlite|https://github.com/sqlite/sqlite.git"
  "tinygltf|https://github.com/syoyo/tinygltf.git"
  "lcms|https://github.com/mm2/Little-CMS.git"
  "c-ares|https://github.com/c-ares/c-ares.git"
  "libpng|https://github.com/pnggroup/libpng.git"
  "libmagic|https://github.com/file/file.git"
  "re2|https://github.com/google/re2.git"
  "libjpeg-turbo|https://github.com/libjpeg-turbo/libjpeg-turbo.git"
  "libtiff|https://github.com/libsdl-org/libtiff.git"
  "liblouis|https://github.com/liblouis/liblouis.git"
  "libpcap|https://github.com/the-tcpdump-group/libpcap.git"
  "curl|https://github.com/curl/curl.git"
  "protobuf|https://github.com/protocolbuffers/protobuf.git"
  "libaom|https://aomedia.googlesource.com/aom"
  "libvpx|https://chromium.googlesource.com/webm/libvpx"
  "openssl|https://github.com/openssl/openssl.git"
  "opencv|https://github.com/opencv/opencv.git"
)

log() { printf '[%s] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*"; }

index=0
for entry in "${TARGETS[@]}"; do
  index=$((index + 1))
  id="${entry%%|*}"
  url="${entry#*|}"
  derived="$(basename "${url%.git}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g; s/^[^a-z0-9]*//; s/[^a-z0-9]*$//')"
  container="fuzzagent-$derived"
  session="$(printf 'fz-b%02d' "$index")"
  out="$ROOT_DIR/output/7d-${id}-$(date +%Y%m%d-%H%M)"

  if sudo docker ps --format '{{.Names}}' | grep -qx "$container"; then
    log "skip $id ($container already running)"
    continue
  fi
  sudo docker rm -f "$container" >/dev/null 2>&1 || true

  log "launch [$index/${#TARGETS[@]}] $id -> $out ($session)"
  if ./benchmark/run-wrapper.sh launch "$url" --duration "$DURATION" \
        --config config/campaign-codex.yaml \
        --auth-dir "$ROOT_DIR/.cache/auth-codex" \
        --http-proxy "${http_proxy:-}" --https-proxy "${https_proxy:-}" \
        --output "$out" --session "$session" >"$LOG_DIR/$id.launch.log" 2>&1; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$derived" "$container" "$session" "$out" "launched" >> "$MANIFEST"
  else
    log "launch FAILED for $id (see $LOG_DIR/$id.launch.log)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$derived" "$container" "$session" "$out" "failed" >> "$MANIFEST"
  fi

  sleep "$STAGGER"
done

log "all targets launched"
