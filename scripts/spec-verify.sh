#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../spec"

if ! command -v tlc > /dev/null; then
    if ! command -v nix > /dev/null || ! tlaplus=$(nix build --no-link --print-out-paths nixpkgs#tlaplus); then
        echo "spec-verify: needs tlc or nix" >&2
        exit 1
    fi
    export PATH="$tlaplus/bin:$PATH"
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

verify_invariants() {
    local mod="$1"
    shift
    local cfg="$work/${mod}.cfg"
    local meta="$work/states-${mod}"
    printf 'SPECIFICATION Spec\n' > "$cfg"
    for inv in "$@"; do
        printf 'INVARIANT %s\n' "$inv" >> "$cfg"
    done
    local output
    if ! output=$(tlc -config "$cfg" -metadir "$meta" -workers 1 "${mod}.tla" 2>&1); then
        echo "$output"
        echo "spec-verify: $mod invariants failed" >&2
        return 1
    fi
    echo "$mod: $(echo "$output" | grep 'distinct states found')"
}

verify_witness() {
    local mod="$1"
    local inv="$2"
    local cfg="$work/${mod}-${inv}.cfg"
    local meta="$work/states-${mod}-${inv}"
    printf 'SPECIFICATION Spec\nINVARIANT %s\n' "$inv" > "$cfg"
    local output
    if output=$(tlc -config "$cfg" -metadir "$meta" -workers 1 "${mod}.tla" 2>&1); then
        echo "$output"
        echo "spec-verify: $mod: $inv was not violated (state unreachable)" >&2
        return 1
    fi
    if ! echo "$output" | grep -q "Invariant $inv is violated"; then
        echo "$output"
        echo "spec-verify: $mod: TLC did not finish for $inv" >&2
        return 1
    fi
}

pids=()
verify_invariants store_publish_properties TypeOK stagedOnlyAfterHashVerified publishedOnlyAfterVerifyOrHit hashMismatchAborts endsAbsorbing & pids+=($!)
verify_invariants fetch_transport_properties TypeOK noHttpsDowngrade curlOnlyFromHttps doneOnlyFrom200OrCurlOk endsAbsorbing & pids+=($!)
verify_invariants link_guard_properties TypeOK neverClobbersWithoutForce symlinkSameIsNoop endsAbsorbing & pids+=($!)
verify_witness store_publish_properties notPublished & pids+=($!)
verify_witness store_publish_properties notAborted & pids+=($!)
verify_witness fetch_transport_properties notDone & pids+=($!)
verify_witness fetch_transport_properties notCurlHttps & pids+=($!)
verify_witness fetch_transport_properties notFailed & pids+=($!)
verify_witness link_guard_properties notLinked & pids+=($!)
verify_witness link_guard_properties notRefused & pids+=($!)
verify_witness link_guard_properties notUnchanged & pids+=($!)

failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        failed=1
    fi
done
if [ "$failed" -ne 0 ]; then
    exit 1
fi
echo "spec-verify: ok"
