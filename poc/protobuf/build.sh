#!/usr/bin/env bash
# Build the protobuf PoC against the campaign's ASan/UBSan-instrumented build.
set -euo pipefail
OUT="${OUT:?Set OUT to the protobuf build output directory}"
INC="$OUT/build/sanitizer/include"
LIB="$OUT/build/sanitizer/lib"
clang++ -g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined \
  -I"$INC" \
  poc.cc -o poc \
  -Wl,--start-group "$LIB"/*.a -Wl,--end-group \
  -lpthread -ldl -lz
echo "built: poc"
