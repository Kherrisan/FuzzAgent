#!/usr/bin/env bash
# Provision the campaign container for a wolfSSL build.
#
# Runs once, inside the container, before any agent starts (see
# `setup_script` in config/campaign-codex.yaml). Any non-zero exit stops the
# campaign, so every step below must be strict.
#
# wolfSSL is self-contained (it ships its own crypto), so this only needs the
 # autotools chain plus a C/C++ toolchain; the fuzzing instrumentation
# (clang, libFuzzer, ASan) is already baked into the image.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Archive access is the one thing that breaks in this environment, so route
# apt through whatever proxy the campaign was launched with (apt does not
# reliably pick up http_proxy from the environment by itself).
if [[ -n "${http_proxy:-}" ]]; then
    printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
        "$http_proxy" "${https_proxy:-$http_proxy}" > /etc/apt/apt.conf.d/99fuzzagent-proxy
fi

for attempt in 1 2 3; do
    if apt-get update -qq; then
        break
    fi
    if [[ "$attempt" -eq 3 ]]; then
        echo "apt-get update failed after $attempt attempts" >&2
        exit 1
    fi
    echo "apt-get update failed (attempt $attempt), retrying..." >&2
    sleep 5
done

apt-get install -y --no-install-recommends \
    build-essential \
    autoconf \
    automake \
    libtool \
    pkg-config \
    perl \
    ca-certificates

# Fail loudly if the toolchain the build script expects is not there.
missing=()
for tool in gcc g++ clang clang++ make autoconf automake libtoolize pkg-config perl; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing build tools: ${missing[*]}" >&2
    exit 1
fi

echo "wolfssl provisioning complete"
