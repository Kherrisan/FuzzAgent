#!/usr/bin/env bash
# Container entrypoint: set up the campaign tree and hand over to campaignctl.
set -uo pipefail

BENCHMARK_DURATION="${FUZZING_DURATION:-24h}"
CONFIG="${FUZZAGENT_CONFIG:-config/campaign-codex.yaml}"

# Keep a pristine copy of the target source inside the campaign output and work
# on a symlink, so a build that writes into $SRC cannot corrupt the checkout.
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

echo "Starting FuzzAgent benchmark ($CONFIG) for $BENCHMARK_DURATION."
resume_args=()
if [[ "${FUZZAGENT_RESUME:-0}" == 1 ]]; then
    resume_args+=(--resume)
fi
CTL=campaignctl
if [[ -x /root/FuzzAgent/bin/campaignctl ]]; then
    CTL=/root/FuzzAgent/bin/campaignctl
fi
bash /root/FuzzAgent/src/tools/copy_tools.sh
timeout --signal=INT --kill-after=60s "$BENCHMARK_DURATION" \
    "$CTL" --config "$CONFIG" up "${resume_args[@]}"
status=$?
# 124 = time budget spent, 130 = INT delivered by `timeout`; both end the
# campaign normally. Anything else (including 3, a failed provisioning script)
# must stop the container so the operator sees it.
if [[ $status -ne 0 && $status -ne 124 && $status -ne 130 ]]; then
    printf 'campaignctl exited with status %s; campaign stopped.\n' "$status" >&2
    exit "$status"
fi
exit 0
