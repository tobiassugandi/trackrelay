#!/usr/bin/env bash

set -euo pipefail

api_image="${1:-trackrelay-api:local}"
postgres_image="${TRACKRELAY_POSTGRES_IMAGE:-postgres:17-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73}"
postgres_pull_policy="${TRACKRELAY_POSTGRES_PULL_POLICY:-missing}"
docker_command="${DOCKER:-docker}"
uv_command="${UV:-uv}"
compose_file="deploy/rehost/compose.yaml"
project_name="trackrelay-rehost-smoke-$$"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/trackrelay-rehost-smoke.XXXXXX")"
runtime_environment="${temporary_directory}/runtime.env"
cleanup_failed=0
stack_started=0

compose() {
    "${docker_command}" compose \
        --project-name "${project_name}" \
        --env-file "${runtime_environment}" \
        --file "${compose_file}" \
        "$@"
}

cleanup() {
    local smoke_status=$?

    if (( smoke_status != 0 && stack_started != 0 )); then
        printf 'Rehost smoke test failed; service status and logs follow.\n' >&2
        compose ps --all >&2 || true
        compose logs --no-color >&2 || true
    fi

    if (( stack_started != 0 )); then
        if ! compose down \
            --volumes \
            --remove-orphans \
            --timeout 10 \
            >/dev/null 2>&1; then
            printf 'Failed to remove the isolated rehost smoke stack.\n' >&2
            cleanup_failed=1
        fi
    fi
    rm -f "${runtime_environment}"
    rmdir "${temporary_directory}" 2>/dev/null || true

    if (( smoke_status == 0 && cleanup_failed != 0 )); then
        exit 1
    fi
}
trap cleanup EXIT

wait_for_api_readiness() {
    local api_url=$1

    for _ in {1..45}; do
        if [[ "$(
            curl \
                --silent \
                --show-error \
                "${api_url}/health/ready" \
                2>/dev/null || true
        )" == '{"status":"ready"}' ]]; then
            return 0
        fi
        sleep 1
    done
    printf 'API did not become ready at %s.\n' "${api_url}" >&2
    return 1
}

discover_api_url() {
    local api_binding

    api_binding="$(compose port api 8000)"
    printf 'http://127.0.0.1:%s\n' "${api_binding##*:}"
}

"${docker_command}" info >/dev/null
"${docker_command}" compose version >/dev/null

printf 'Building application image %s...\n' "${api_image}"
if [[ "${REHOST_SKIP_BUILD:-false}" == "true" ]]; then
    "${docker_command}" image inspect "${api_image}" >/dev/null
    printf 'Using explicitly requested cached application image.\n'
else
    "${docker_command}" build --file Dockerfile --tag "${api_image}" .
fi

database_password="$(${uv_command} run --locked python -c \
    'import secrets; print(secrets.token_hex(24))')"
{
    printf 'TRACKRELAY_API_IMAGE=%s\n' "${api_image}"
    printf 'TRACKRELAY_IMAGE_PULL_POLICY=never\n'
    printf 'TRACKRELAY_POSTGRES_IMAGE=%s\n' "${postgres_image}"
    printf 'TRACKRELAY_POSTGRES_PULL_POLICY=%s\n' "${postgres_pull_policy}"
    printf 'TRACKRELAY_API_BIND_ADDRESS=127.0.0.1\n'
    printf 'TRACKRELAY_API_PORT=0\n'
    printf 'POSTGRES_DB=trackrelay\n'
    printf 'POSTGRES_USER=trackrelay\n'
    printf 'POSTGRES_PASSWORD=%s\n' "${database_password}"
    printf 'TRACKRELAY_DATABASE_URL=postgresql+psycopg://trackrelay:%s@postgres:5432/trackrelay\n' "${database_password}"
    printf 'TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS=5.0\n'
} >"${runtime_environment}"
chmod 600 "${runtime_environment}"
unset database_password

printf 'Starting isolated rehost stack...\n'
stack_started=1
compose up --detach --wait --wait-timeout 90 postgres downstream
compose run --rm --no-deps migrate alembic upgrade head
compose up --detach --wait --wait-timeout 90 api

migration_status="$(
    compose run --rm --no-deps migrate alembic current
)"
if [[ "${migration_status}" != *"0006_test_runs (head)"* ]]; then
    printf 'Unexpected migration status: %s\n' "${migration_status}" >&2
    exit 1
fi

api_url="$(discover_api_url)"
wait_for_api_readiness "${api_url}"

api_uid="$(compose exec -T api id -u)"
downstream_uid="$(compose exec -T downstream id -u)"
if [[ "${api_uid}" != "10001" || "${downstream_uid}" != "10001" ]]; then
    printf 'Application containers did not run as UID 10001.\n' >&2
    exit 1
fi

printf 'Running all four correctness scenarios inside the private network...\n'
correctness_evidence="$(
    compose run --rm --no-deps api \
        python -m trackrelay.experiments.rds_correctness \
        --suite-id 00000000-0000-0000-0000-000000000922
)"
printf '%s\n' "${correctness_evidence}" | \
    "${uv_command}" run --locked python -c '
import base64
import json
import sys

prefix = "TRACKRELAY_RDS_CORRECTNESS_EVIDENCE="
evidence_lines = [
    line.removeprefix(prefix)
    for line in sys.stdin.read().splitlines()
    if line.startswith(prefix)
]
assert len(evidence_lines) == 1
evidence = json.loads(base64.b64decode(evidence_lines[0], validate=True))
assert evidence["all_scenarios_passed"] is True
assert [
    result["scenario"] for result in evidence["scenario_results"]
] == ["normal", "duplicate", "out-of-order", "downstream-outage"]
assert all(
    result["reconciliation"]["invariants_passed"] is True
    for result in evidence["scenario_results"]
)
'

compose run --rm --no-deps api python -c '
from trackrelay.database import session_factory
from trackrelay.models import Partner

with session_factory.begin() as session:
    session.add(
        Partner(
            id="smoke-alpha",
            name="Smoke Alpha",
            adapter_type="courier-alpha",
            is_active=True,
        )
    )
' >/dev/null

ingestion_response="$(
    curl \
        --fail-with-body \
        --silent \
        --show-error \
        --request POST \
        --header 'Content-Type: application/json' \
        --data '{"event_id":"SMOKE-EVENT-001","tracking_number":"SMOKE-TRACKING-001","status":"PICKUP","event_time":"2026-08-26T09:00:00+07:00"}' \
        "${api_url}/api/v1/partners/smoke-alpha/events"
)"
event_id="$(
    printf '%s' "${ingestion_response}" | \
        "${uv_command}" run --locked python -c '
import json
import sys

response = json.load(sys.stdin)
assert response["processing_status"] == "processed"
assert response["duplicate"] is False
assert response["delivery_status"] == "delivered"
assert response["downstream_status_code"] == 202
print(response["event_id"])
'
)"

downstream_receipts="$(
    compose exec -T downstream python -c '
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8001/events", timeout=2) as response:
    print(json.dumps(json.load(response)))
'
)"
printf '%s' "${downstream_receipts}" | \
    "${uv_command}" run --locked python -c '
import json
import sys

receipts = json.load(sys.stdin)
smoke_receipts = [
    receipt
    for receipt in receipts
    if receipt["partner_id"] == "smoke-alpha"
    and receipt["partner_event_id"] == "SMOKE-EVENT-001"
]
assert len(smoke_receipts) == 1
assert smoke_receipts[0]["tracking_number"] == "SMOKE-TRACKING-001"
assert smoke_receipts[0]["status"] == "picked_up"
'

printf 'Restarting PostgreSQL and API to prove durable persistence...\n'
compose restart --timeout 10 postgres api >/dev/null
api_url="$(discover_api_url)"
wait_for_api_readiness "${api_url}"

persisted_event="$(
    curl \
        --fail \
        --silent \
        --show-error \
        "${api_url}/api/v1/events/${event_id}"
)"
printf '%s' "${persisted_event}" | \
    "${uv_command}" run --locked python -c '
import json
import sys

event = json.load(sys.stdin)
assert event["partner_id"] == "smoke-alpha"
assert event["partner_event_id"] == "SMOKE-EVENT-001"
assert event["tracking_number"] == "SMOKE-TRACKING-001"
assert event["status"] == "picked_up"
assert event["state_applied"] is True
assert len(event["delivery_attempts"]) == 1
assert event["delivery_attempts"][0]["result"] == "delivered"
'

shipment="$(
    curl \
        --fail \
        --silent \
        --show-error \
        "${api_url}/api/v1/shipments/SMOKE-TRACKING-001"
)"
printf '%s' "${shipment}" | \
    "${uv_command}" run --locked python -c '
import json
import sys

shipment = json.load(sys.stdin)
assert shipment["tracking_number"] == "SMOKE-TRACKING-001"
assert shipment["current_status"] == "picked_up"
'

printf 'Rehost smoke test passed.\n'
printf 'Migration: 0006_test_runs (head)\n'
printf 'API and downstream UID: 10001\n'
printf 'Core correctness scenarios: 4 passed\n'
printf 'Persisted event: SMOKE-EVENT-001\n'
printf 'Persisted shipment state: picked_up\n'
