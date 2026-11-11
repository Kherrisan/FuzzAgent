#!/usr/bin/env bash
set -uo pipefail

BENCHMARK_DURATION="${FUZZING_DURATION:-24h}"
ENGINE="${FUZZAGENT_ENGINE:-legacy}"
CONFIG="${FUZZAGENT_CONFIG:-config/campaign.yaml}"

if [[ "$ENGINE" == "hc" ]]; then
    saved_source="$OUTPUT/.campaign/source"
    if [[ ! -d "$saved_source" ]]; then
        mkdir -p "$OUTPUT/.campaign"
        cp -a "/src/${REPO_BASE_NAME}" "$saved_source" || exit 1
    fi
    if ! grep -qxF '.campaign/source/' "$OUTPUT/.gitignore" 2>/dev/null; then
        printf '\n.campaign/source/\n' >> "$OUTPUT/.gitignore"
    fi
    if [[ ! -d "$SRC" ]]; then
        mkdir -p "$(dirname "$SRC")"
        ln -s "$saved_source" "$SRC" || exit 1
    fi
    echo "Starting FuzzAgent benchmark (hc runtime, $CONFIG) for $BENCHMARK_DURATION."
    resume_args=()
    if [[ "${FUZZAGENT_RESUME:-0}" == 1 ]]; then
        resume_args+=(--resume)
    fi
    exec timeout --signal=INT --kill-after=60s "$BENCHMARK_DURATION" \
        campaignctl --config "$CONFIG" up "${resume_args[@]}"
fi

echo "Starting FuzzAgent benchmark for $BENCHMARK_DURATION."
BAKED_PY=/opt/fuzzagent-build/.venv/bin/python
if [[ -x "$BAKED_PY" ]]; then
    timeout --signal=INT --kill-after=60s "$BENCHMARK_DURATION" "$BAKED_PY" main.py
else
    timeout --signal=INT --kill-after=60s "$BENCHMARK_DURATION" uv run main.py
fi
FUZZ_EXIT_CODE=$?

if [[ "$FUZZ_EXIT_CODE" -ne 0 && "$FUZZ_EXIT_CODE" -ne 124 ]]; then
    echo "FuzzAgent exited with status $FUZZ_EXIT_CODE; skipping final coverage export." >&2
    exit "$FUZZ_EXIT_CODE"
fi

echo "Benchmark complete. Exporting full code coverage."
bash /root/FuzzAgent/src/tools/copy_tools.sh
exec python3 -m introspector
