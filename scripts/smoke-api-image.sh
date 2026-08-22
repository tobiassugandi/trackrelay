#!/usr/bin/env bash

set -euo pipefail

image_name="${1:-trackrelay-api:local}"
docker_command="${DOCKER:-docker}"
container_name="trackrelay-api-smoke-$$"

cleanup() {
    "${docker_command}" container rm --force "${container_name}" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${docker_command}" run \
    --detach \
    --name "${container_name}" \
    --publish 127.0.0.1::8000 \
    --rm \
    "${image_name}" \
    >/dev/null

health_status="starting"
for _ in {1..30}; do
    health_status="$(
        "${docker_command}" inspect \
            --format '{{.State.Health.Status}}' \
            "${container_name}"
    )"
    if [[ "${health_status}" == "healthy" ]]; then
        break
    fi
    if [[ "${health_status}" == "unhealthy" ]]; then
        break
    fi
    sleep 1
done

if [[ "${health_status}" != "healthy" ]]; then
    "${docker_command}" logs "${container_name}" >&2
    printf 'container health status: %s\n' "${health_status}" >&2
    exit 1
fi

published_address="$(
    "${docker_command}" port "${container_name}" 8000/tcp | head -n 1
)"
published_port="${published_address##*:}"
liveness_response="$(
    curl \
        --fail \
        --silent \
        --show-error \
        "http://127.0.0.1:${published_port}/health/live"
)"
container_user_id="$("${docker_command}" exec "${container_name}" id -u)"

if [[ "${liveness_response}" != '{"status":"ok"}' ]]; then
    printf 'unexpected liveness response: %s\n' "${liveness_response}" >&2
    exit 1
fi
if [[ "${container_user_id}" != "10001" ]]; then
    printf 'container runs as unexpected UID: %s\n' "${container_user_id}" >&2
    exit 1
fi

printf 'API image smoke test passed\n'
printf 'liveness: %s\n' "${liveness_response}"
printf 'runtime UID: %s\n' "${container_user_id}"
