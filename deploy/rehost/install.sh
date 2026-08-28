#!/usr/bin/env bash

set -euo pipefail

: "${TRACKRELAY_API_IMAGE:?TRACKRELAY_API_IMAGE must be set}"
: "${TRACKRELAY_AWS_REGION:?TRACKRELAY_AWS_REGION must be set}"
: "${TRACKRELAY_POSTGRES_IMAGE:?TRACKRELAY_POSTGRES_IMAGE must be set}"
: "${TRACKRELAY_SMOKE_SUFFIX:?TRACKRELAY_SMOKE_SUFFIX must be set}"

cloud-init status --wait
docker compose version >/dev/null

deployment_directory="/opt/trackrelay"
runtime_environment="${deployment_directory}/.env"
compose_file="${deployment_directory}/compose.yaml"
registry_host="${TRACKRELAY_API_IMAGE%%/*}"

logout_registry() {
    docker logout "${registry_host}" >/dev/null 2>&1 || true
}
trap logout_registry EXIT

if [[ ! -f "${compose_file}" ]]; then
    printf 'Rehost Compose file is missing.\n' >&2
    exit 1
fi

database_password=""
if [[ -f "${runtime_environment}" ]]; then
    database_password="$(
        awk -F= \
            '$1 == "POSTGRES_PASSWORD" {print substr($0, index($0, "=") + 1)}' \
            "${runtime_environment}"
    )"
fi
if [[ ! "${database_password}" =~ ^[0-9a-f]{48}$ ]]; then
    database_password="$(openssl rand -hex 24)"
fi

umask 077
runtime_environment_temporary="$(mktemp "${deployment_directory}/.env.XXXXXX")"
{
    printf 'TRACKRELAY_API_IMAGE=%s\n' "${TRACKRELAY_API_IMAGE}"
    printf 'TRACKRELAY_IMAGE_PULL_POLICY=always\n'
    printf 'TRACKRELAY_POSTGRES_IMAGE=%s\n' "${TRACKRELAY_POSTGRES_IMAGE}"
    printf 'TRACKRELAY_POSTGRES_PULL_POLICY=missing\n'
    printf 'TRACKRELAY_API_BIND_ADDRESS=0.0.0.0\n'
    printf 'TRACKRELAY_API_PORT=8000\n'
    printf 'POSTGRES_DB=trackrelay\n'
    printf 'POSTGRES_USER=trackrelay\n'
    printf 'POSTGRES_PASSWORD=%s\n' "${database_password}"
    printf 'TRACKRELAY_DATABASE_URL=postgresql+psycopg://trackrelay:%s@postgres:5432/trackrelay\n' "${database_password}"
    printf 'TRACKRELAY_DATABASE_POOL_SIZE=5\n'
    printf 'TRACKRELAY_DATABASE_MAX_OVERFLOW=10\n'
    printf 'TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS=5.0\n'
} >"${runtime_environment_temporary}"
chmod 0600 "${runtime_environment_temporary}"
mv "${runtime_environment_temporary}" "${runtime_environment}"
unset database_password

compose() {
    docker compose \
        --project-name trackrelay-rehost \
        --env-file "${runtime_environment}" \
        --file "${compose_file}" \
        "$@"
}

aws ecr get-login-password --region "${TRACKRELAY_AWS_REGION}" | \
    docker login \
        --username AWS \
        --password-stdin \
        "${registry_host}" \
        >/dev/null

compose pull --quiet
compose up --detach --wait --wait-timeout 120 postgres downstream
compose run --rm --no-deps migrate alembic upgrade head
compose up --detach --wait --wait-timeout 120 api

migration_status="$(compose run --rm --no-deps migrate alembic current)"
if [[ "${migration_status}" != *"0006_test_runs (head)"* ]]; then
    printf 'Unexpected migration status.\n' >&2
    exit 1
fi

if [[ "$(curl --fail --silent http://127.0.0.1:8000/health/ready)" != \
    '{"status":"ready"}' ]]; then
    printf 'TrackRelay API is not ready.\n' >&2
    exit 1
fi

partner_event_id="CLOUD-SMOKE-${TRACKRELAY_SMOKE_SUFFIX}"
tracking_number="CLOUD-SMOKE-TRK-${TRACKRELAY_SMOKE_SUFFIX}"
compose run --rm --no-deps api python -c '
from trackrelay.database import session_factory
from trackrelay.models import Partner

with session_factory.begin() as session:
    partner = session.get(Partner, "cloud-smoke-alpha")
    if partner is None:
        session.add(
            Partner(
                id="cloud-smoke-alpha",
                name="Cloud Smoke Alpha",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )
    else:
        partner.adapter_type = "courier-alpha"
        partner.is_active = True
' >/dev/null

smoke_response="$(
    curl \
        --fail-with-body \
        --silent \
        --show-error \
        --request POST \
        --header 'Content-Type: application/json' \
        --data "{\"event_id\":\"${partner_event_id}\",\"tracking_number\":\"${tracking_number}\",\"status\":\"PICKUP\",\"event_time\":\"2026-08-26T09:00:00+07:00\"}" \
        http://127.0.0.1:8000/api/v1/partners/cloud-smoke-alpha/events
)"
printf '%s' "${smoke_response}" | compose exec -T api python -c '
import json
import sys

response = json.load(sys.stdin)
assert response["processing_status"] == "processed"
assert response["duplicate"] is False
assert response["delivery_status"] == "delivered"
assert response["downstream_status_code"] == 202
'

shipment_response="$(
    curl \
        --fail \
        --silent \
        --show-error \
        "http://127.0.0.1:8000/api/v1/shipments/${tracking_number}"
)"
printf '%s' "${shipment_response}" | compose exec -T api python -c '
import json
import sys

shipment = json.load(sys.stdin)
assert shipment["current_status"] == "picked_up"
'

printf 'TRACKRELAY_REHOST_READY\n'
