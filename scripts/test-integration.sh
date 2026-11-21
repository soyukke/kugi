#!/bin/sh
# Runs end-to-end CLI integration tests against a local loopback mock HTTP server.
set -eu
cd "$(dirname "$0")/.."

if [ ! -x dist/kugi ]; then
  sh scripts/build.sh
fi

python3 tests/integration.py dist/kugi
