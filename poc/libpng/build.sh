#!/usr/bin/env bash
# Build the libpng PoC against the campaign's ASan/UBSan-instrumented build.
set -euo pipefail
OUT="${OUT:-/root/FuzzAgent/output/libpng}"
INC="$OUT/build/sanitizer/include"
LIB="$OUT/build/sanitizer/lib"
clang++ -g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined \
  -I"$INC" -I"$OUT/build/sanitizer/3rdparty/include" \
  poc.cc -o poc \
  -Wl,--start-group "$LIB/libpng.a" "$LIB/libpng18.a" -Wl,--end-group \
  -lz -lpthread
echo "built: $(pwd)/poc"
