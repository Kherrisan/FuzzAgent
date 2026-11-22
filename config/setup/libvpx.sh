#!/usr/bin/env bash
# Provision the container for a libvpx build (autotools; optional assembler).
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
    build-essential autoconf automake libtool pkg-config \
    nasm yasm perl ca-certificates

missing=()
for tool in gcc g++ clang clang++ make autoconf automake libtoolize pkg-config perl nasm; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
# yasm is optional for libvpx (nasm is enough).
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing build tools: ${missing[*]}" >&2
    exit 1
fi
echo "libvpx provisioning complete"
