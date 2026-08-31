#!/usr/bin/env bash

set -euo pipefail

image_name="${1:-trackrelay-worker:local}"
docker_command="${DOCKER:-docker}"

configured_command="$(
    "${docker_command}" image inspect \
        --format '{{json .Config.Cmd}}' \
        "${image_name}"
)"
if [[ "${configured_command}" != '["trackrelay-worker"]' ]]; then
    printf 'worker image has unexpected command: %s\n' \
        "${configured_command}" >&2
    exit 1
fi

probe_output="$(
    "${docker_command}" run \
        --rm \
        --network none \
        --entrypoint python \
        "${image_name}" \
        -c \
        'import os, shutil; from trackrelay.worker import main; assert callable(main); assert shutil.which("trackrelay-worker"); print(os.getuid())'
)"
if [[ "${probe_output}" != "10001" ]]; then
    printf 'worker image probe returned unexpected output: %s\n' \
        "${probe_output}" >&2
    exit 1
fi

set +e
startup_output="$(
    "${docker_command}" run --rm --network none "${image_name}" 2>&1
)"
startup_status=$?
set -e
if [[ ${startup_status} -eq 0 ]]; then
    printf 'worker unexpectedly started without SQS configuration\n' >&2
    exit 1
fi
if [[ "${startup_output}" != *"TRACKRELAY_DELIVERY_QUEUE_BACKEND=sqs"* ]]; then
    printf 'worker did not reject missing SQS configuration as expected\n' >&2
    printf '%s\n' "${startup_output}" >&2
    exit 1
fi

printf 'worker image smoke test passed\n'
printf 'configured command: %s\n' "${configured_command}"
printf 'runtime UID: %s\n' "${probe_output}"
printf 'missing SQS configuration: rejected\n'
