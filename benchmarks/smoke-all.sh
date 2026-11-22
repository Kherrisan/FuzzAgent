#!/usr/bin/env bash
# Sequential 30-minute smoke test over the benchmark targets.
#
# Ordered simple -> complex so cheap targets surface harness/build bugs first.
# Each target: launch -> wait for the container to exit -> record one TSV row.
# Re-running skips targets already present in summary.tsv.
#
#   SMOKE_DURATION=30m benchmarks/smoke-all.sh
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DURATION="${SMOKE_DURATION:-30m}"
RESULTS_DIR="${SMOKE_RESULTS:-benchmarks/smoke-results}"
SUMMARY="$RESULTS_DIR/summary.tsv"
mkdir -p "$RESULTS_DIR"
[[ -f "$SUMMARY" ]] || printf 'target\tstatus\tharness\tfuzzer\tcrash\tbuild\trounds\tlast_event\toutput\n' > "$SUMMARY"

# id|repository url  (id is the benchmark directory name, not the derived one)
TARGETS=(
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

  if cut -f1 "$SUMMARY" 2>/dev/null | grep -qx "$id"; then
    log "skip $id (already recorded)"
    continue
  fi

  session="$(printf 'fz-s%02d' "$index")"
  derived="$(basename "${url%.git}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g; s/^[^a-z0-9]*//; s/[^a-z0-9]*$//')"
  container="fuzzagent-$derived"
  out="$ROOT_DIR/output/smoke-${id}-$(date +%Y%m%d-%H%M)"

  log "=== [$index/${#TARGETS[@]}] $id  ($url) -> $out"
  sudo docker rm -f "$container" >/dev/null 2>&1 || true

  if ! ./benchmark/run-wrapper.sh launch "$url" --duration "$DURATION" \
        --config config/campaign-codex.yaml \
        --auth-dir "$ROOT_DIR/.cache/auth-codex" \
        --http-proxy "${http_proxy:-}" --https-proxy "${https_proxy:-}" \
        --output "$out" --session "$session" >"$RESULTS_DIR/$id.launch.log" 2>&1; then
    log "launch failed for $id"
    printf '%s\tlaunch-failed\t0\t0\t0\t?\t0\t-\t%s\n' "$id" "$out" >> "$SUMMARY"
    continue
  fi

  # Wait until the container is gone (budget + up to 20 min for the last round).
  for _ in $(seq 1 200); do
    if ! sudo docker ps --format '{{.Names}}' | grep -qx "$container"; then break; fi
    sleep 15
  done

  journal="$out/.campaign/journal.jsonl"
  harness=$(ls "$out/harnesses" 2>/dev/null | wc -l)
  fuzzer=$(ls "$out/fuzzers" 2>/dev/null | wc -l)
  crash=$(find "$out/fuzzers" -path '*/crashes/crash-*' 2>/dev/null | wc -l)
  build=$([ "$fuzzer" -gt 0 ] && echo ok || echo fail)
  rounds=$(grep -c '"event": "round_finished"' "$journal" 2>/dev/null)
rounds=${rounds:-0}
  last=$(tail -1 "$journal" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["event"])' 2>/dev/null || echo "-")
  status=ok
  [ "$harness" -eq 0 ] && status=no-harness
  [ "$build" = fail ] && status=no-build

  log "$id done: harness=$harness fuzzer=$fuzzer crash=$crash rounds=$rounds last=$last status=$status"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$id" "$status" "$harness" "$fuzzer" "$crash" "$build" "$rounds" "$last" "$out" >> "$SUMMARY"
done

log "all targets processed; summary: $SUMMARY"
