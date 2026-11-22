#!/usr/bin/env bash
# Build and run the libpng PoC, saving the sanitizer report next to it.
set -euo pipefail
cd "$(dirname "$0")"
bash build.sh
./poc input.png > asan-report.txt 2>&1 || true
echo "--- report (head) ---"
head -20 asan-report.txt
