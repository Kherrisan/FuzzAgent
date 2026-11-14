#!/usr/bin/env bash
# Provision the campaign container for a GnuTLS build.
#
# Runs once, inside the container, before any agent starts (see
# `setup_script` in config/campaign-codex.yaml). Any non-zero exit stops the
# campaign, so every step below must be strict.
#
# GnuTLS is an autotools project that bootstraps itself from git
 # (`./bootstrap`), which needs gperf, bison, gtk-doc and autopoint, plus the
 # -dev packages its bundled dependencies (nettle, libtasn1, libidn2, p11-kit)
 # are detected through. Without these the build fails in autoreconf or,
 # worse, the agent starts hand-patching generated files.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

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
    gettext \
    autopoint \
    bison \
    gperf \
    gtk-doc-tools \
    texinfo \
    libgmp-dev \
    nettle-dev \
    libtasn1-6-dev \
    libp11-kit-dev \
    libidn2-dev \
    libunistring-dev \
    libev4 \
    guile-3.0 \
    python3-six \
    perl \
    ca-certificates

# `./bootstrap` hard-fails when any of these is missing, so check them up front
# rather than discovering it three rounds into a build repair.
missing=()
for tool in gcc g++ clang clang++ make autoconf automake libtoolize pkg-config \
            aclocal autoheader autopoint gettextize msgfmt gperf bison gtkdocize \
            makeinfo; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing build tools: ${missing[*]}" >&2
    exit 1
fi

# The -dev packages are what the configure checks look for.
missing_pkgs=()
for pkg in nettle-dev libtasn1-6-dev libp11-kit-dev libidn2-dev libunistring-dev libgmp-dev; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing_pkgs+=("$pkg")
done
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    echo "missing dev packages: ${missing_pkgs[*]}" >&2
    exit 1
fi

echo "gnutls provisioning complete"
