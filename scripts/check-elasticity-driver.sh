#!/bin/sh
set -eu

trackrelay_elasticity_root="$(
  CDPATH= cd -- "$(dirname -- "$0")/.." && pwd
)"
trackrelay_elasticity_tmp="$(mktemp -d)"
trap 'rm -rf -- "$trackrelay_elasticity_tmp"' EXIT HUP INT TERM

"${UV:-uv}" run --locked trackrelay-prepare-elasticity \
  --treatment fixed \
  --output-directory "$trackrelay_elasticity_tmp/fixed"

ELASTICITY_TEST_INPUT_DIR="$trackrelay_elasticity_tmp/fixed" \
  "${NODE:-node}" --experimental-vm-modules --test \
  "$trackrelay_elasticity_root/scripts/test-elasticity-driver.mjs"

"${DOCKER:-docker}" run --rm \
  --env K6_MANIFEST_PATH=/input-manifest.json \
  --env K6_WORKLOAD_PATH=/workload-definition.json \
  --volume "$trackrelay_elasticity_root/load:/scripts:ro" \
  --volume "$trackrelay_elasticity_tmp/fixed/input-manifest.json:/input-manifest.json:ro" \
  --volume "$trackrelay_elasticity_tmp/fixed/workload-definition.json:/workload-definition.json:ro" \
  "${K6_IMAGE:-grafana/k6:2.1.0}" \
  inspect --include-system-env-vars /scripts/elasticity-steps.js >/dev/null

printf '%s\n' 'Elasticity workload and k6 driver: valid'
