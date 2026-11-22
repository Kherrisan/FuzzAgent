#!/usr/bin/env bash
# Provision the container for an OpenSSL build (perl + make).
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
    build-essential perl make pkg-config ca-certificates

missing=()
for tool in gcc g++ clang clang++ make perl; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing build tools: ${missing[*]}" >&2
    exit 1
fi
echo "openssl provisioning complete"
