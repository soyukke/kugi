#!/bin/sh
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

"$KIZU" run . -- --dump-machines | python3 scripts/machine-tla.py \
  spec/store_publish.tla \
  spec/fetch_transport.tla \
  spec/link_guard.tla

sh scripts/spec-verify.sh
