set shell := ["sh", "-eu", "-c"]

default: check

lint:
    sh scripts/lint.sh

check: lint

fmt:
    kizu_bin="${KIZU:-kizu}"
    for f in $(find src -name '*.kizu' -type f | sort); do
      "$kizu_bin" fmt --write "$f"
    done

test:
    kizu_bin="${KIZU:-kizu}"
    "$kizu_bin" test .

test-integration:
    sh scripts/test-integration.sh

build:
    sh scripts/build.sh

spec-gen:
    sh scripts/spec-gen.sh

spec-verify:
    sh scripts/spec-verify.sh
