#!/bin/sh
# Runs formatting, hygiene, typecheck, and test gates for kugi.
# Usage:
#   KIZU=/path/to/kizu KIZU_LIB_DIR=/path/to/kizu/lib/kizu sh scripts/lint.sh
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

echo "==> Checking Kizu formatting"
fmt_failed=0
for file in $(find src -name '*.kizu' -type f | sort); do
  if ! "$KIZU" fmt "$file" | diff -u "$file" -; then
    echo "error: $file is not formatted (run: KIZU=$KIZU $KIZU fmt --write $file)" >&2
    fmt_failed=1
  fi
done
if [ "$fmt_failed" -ne 0 ]; then
  exit 1
fi

echo "==> Checking source hygiene"
python3 - <<'PY'
import pathlib, re, sys

errors = []
root = pathlib.Path(".")
text_files = sorted(
    [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "dist" not in p.parts and "target" not in p.parts]
)

abs_home_re = re.compile(r"(/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|/Volumes/[A-Za-z0-9._-]+/)")

for p in text_files:
    raw = p.read_bytes()
    if b"\x00" in raw:
        continue
    text = raw.decode("utf-8")
    if text and not text.endswith("\n"):
        errors.append(f"{p}: missing trailing newline")
    for idx, line in enumerate(text.splitlines(), 1):
        if line.endswith((" ", "\t")):
            errors.append(f"{p}:{idx}: trailing whitespace")
        if p.as_posix() not in ("scripts/lint.sh", ".github/workflows/ci.yml") and abs_home_re.search(line):
            errors.append(f"{p}:{idx}: hardcoded local host path found")

if errors:
    for e in errors:
        print(f"lint error: {e}", file=sys.stderr)
    sys.exit(1)
print("hygiene: ok")
PY

echo "==> Linting Kizu sources"
"$KIZU" lint .

echo "==> Checking Kizu package"
"$KIZU" check .

echo "==> Running Kizu tests"
"$KIZU" test .
