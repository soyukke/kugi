#!/bin/sh
# Builds the optimized native kugi binary into dist/kugi.
# Usage:
#   KIZU=/path/to/kizu KIZU_LIB_DIR=/path/to/kizu/lib/kizu sh scripts/build.sh
set -eu
cd "$(dirname "$0")/.."

if [ -z "${KIZU:-}" ]; then
  if command -v kizu >/dev/null 2>&1; then
    KIZU="$(command -v kizu)"
  else
    echo "error: kizu binary not found in PATH; set KIZU=/path/to/kizu" >&2
    exit 1
  fi
fi

if [ -n "${KIZU_LIB_DIR:-}" ]; then
  export KIZU_LIB_DIR
fi

mkdir -p dist
"$KIZU" build --target native --opt -o dist/kugi .
ls -lh dist/kugi
