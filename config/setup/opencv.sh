#!/usr/bin/env bash
# Provision the container for an OpenCV build.
#
# A full OpenCV build is far too slow for a fuzzing campaign, so this only
# installs what a minimal (core + imgproc + imgcodecs) build needs.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [[ -n "${http_proxy:-}" ]]; then
    printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
        "$http_proxy" "${https_proxy:-$http_proxy}" > /etc/apt/apt.conf.d/99fuzzagent-proxy
fi

for attempt in 1 2 3; do
    apt-get update -qq && break
    [[ "$attempt" -eq 3 ]] && { echo "apt-get update failed" >&2; exit 1; }
    sleep 5
done

apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build pkg-config python3 python3-dev \
    zlib1g-dev libjpeg-dev libpng-dev libtiff-dev ca-certificates

missing=()
for tool in gcc g++ clang clang++ cmake ninja pkg-config python3; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing build tools: ${missing[*]}" >&2
    exit 1
fi
echo "opencv provisioning complete"
