#!/usr/bin/env bash

set -euo pipefail

: "${TRACKRELAY_API_IMAGE:?TRACKRELAY_API_IMAGE must be set}"
: "${TRACKRELAY_AWS_REGION:?TRACKRELAY_AWS_REGION must be set}"
: "${TRACKRELAY_RDS_IDENTIFIER:?TRACKRELAY_RDS_IDENTIFIER must be set}"
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

if [[ ! -f "${compose_file}" || ! -f "${runtime_environment}" ]]; then
    printf 'The synchronous rehost must be deployed before the RDS switch.\n' >&2
    exit 1
fi

read -r rds_host rds_port database_name master_username secret_arn < <(
    aws rds describe-db-instances \
        --region "${TRACKRELAY_AWS_REGION}" \
        --db-instance-identifier "${TRACKRELAY_RDS_IDENTIFIER}" \
        --query \
        '[DBInstances[0].Endpoint.Address, DBInstances[0].Endpoint.Port, DBInstances[0].DBName, DBInstances[0].MasterUsername, DBInstances[0].MasterUserSecret.SecretArn]' \
        --output text
)

if [[ ! "${rds_host}" =~ ^[a-zA-Z0-9.-]+$ ]] || \
    [[ "${rds_port}" != "5432" ]] || \
    [[ "${database_name}" != "trackrelay" ]] || \
    [[ "${master_username}" != "trackrelay_admin" ]] || \
    [[ ! "${secret_arn}" =~ ^arn:aws[a-zA-Z-]*:secretsmanager: ]]; then
    printf 'RDS returned unexpected connection metadata.\n' >&2
    exit 1
fi

secret_json="$(
    aws secretsmanager get-secret-value \
        --region "${TRACKRELAY_AWS_REGION}" \
        --secret-id "${secret_arn}" \
        --query SecretString \
        --output text
)"
database_url="$(
    printf '%s' "${secret_json}" | \
        RDS_HOST="${rds_host}" \
        RDS_PORT="${rds_port}" \
        RDS_DATABASE="${database_name}" \
        RDS_USERNAME="${master_username}" \
        python3 -c '
import json
import os
import sys
from urllib.parse import quote

secret = json.load(sys.stdin)
username = os.environ["RDS_USERNAME"]
if secret.get("username") != username:
    raise SystemExit("managed secret username does not match RDS")
password = secret.get("password")
if not isinstance(password, str) or not password:
    raise SystemExit("managed secret has no password")
escaped_username = quote(username, safe="")
escaped_password = quote(password, safe="")
print(
    "postgresql+psycopg://"
    + escaped_username
    + ":"
    + escaped_password
    + "@"
    + os.environ["RDS_HOST"]
    + ":"
    + os.environ["RDS_PORT"]
    + "/"
    + os.environ["RDS_DATABASE"]
    + "?sslmode=require"
)
'
)"

local_database_password="$(
    awk -F= \
        '$1 == "POSTGRES_PASSWORD" {print substr($0, index($0, "=") + 1)}' \
        "${runtime_environment}"
)"
if [[ ! "${local_database_password}" =~ ^[0-9a-f]{48}$ ]]; then
    local_database_password="$(openssl rand -hex 24)"
fi

umask 077
runtime_environment_temporary="$(mktemp "${deployment_directory}/.env.XXXXXX")"
{
    printf 'TRACKRELAY_API_IMAGE=%s\n' "${TRACKRELAY_API_IMAGE}"
    printf 'TRACKRELAY_IMAGE_PULL_POLICY=always\n'
    printf 'TRACKRELAY_POSTGRES_IMAGE=%s\n' \
        "$(awk -F= '$1 == "TRACKRELAY_POSTGRES_IMAGE" {print substr($0, index($0, "=") + 1)}' "${runtime_environment}")"
    printf 'TRACKRELAY_POSTGRES_PULL_POLICY=missing\n'
    printf 'TRACKRELAY_API_BIND_ADDRESS=0.0.0.0\n'
    printf 'TRACKRELAY_API_PORT=8000\n'
    printf 'POSTGRES_DB=trackrelay\n'
    printf 'POSTGRES_USER=trackrelay\n'
    printf 'POSTGRES_PASSWORD=%s\n' "${local_database_password}"
    printf 'TRACKRELAY_DATABASE_URL=%s\n' "${database_url}"
    printf 'TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS=5.0\n'
} >"${runtime_environment_temporary}"
chmod 0600 "${runtime_environment_temporary}"
mv "${runtime_environment_temporary}" "${runtime_environment}"
unset database_url secret_json secret_arn local_database_password

compose() {
    docker compose \
        --project-name trackrelay-rehost \
        --env-file "${runtime_environment}" \
        --file "${compose_file}" \
        "$@"
}

wait_for_api_readiness() {
    for _ in $(seq 1 60); do
        if [[ "$(
            curl \
                --silent \
                --max-time 2 \
                http://127.0.0.1:8000/health/ready \
                2>/dev/null || true
        )" == '{"status":"ready"}' ]]; then
            return 0
        fi
        sleep 1
    done
    printf 'TrackRelay API did not become ready with RDS.\n' >&2
    return 1
}

aws ecr get-login-password --region "${TRACKRELAY_AWS_REGION}" | \
    docker login \
        --username AWS \
        --password-stdin \
        "${registry_host}" \
        >/dev/null

compose pull --quiet api downstream
compose stop api postgres >/dev/null
compose rm --force api migrate postgres >/dev/null
compose up --detach --wait --wait-timeout 120 downstream
compose run --rm --no-deps migrate alembic upgrade head
compose up --detach --force-recreate --wait --wait-timeout 120 api

migration_status="$(compose run --rm --no-deps migrate alembic current)"
if [[ "${migration_status}" != *"0006_test_runs (head)"* ]]; then
    printf 'Unexpected RDS migration status.\n' >&2
    exit 1
fi

wait_for_api_readiness

compose run --rm --no-deps api python -c '
from trackrelay.database import session_factory
from trackrelay.models import Partner

with session_factory.begin() as session:
    partner = session.get(Partner, "rds-smoke-alpha")
    if partner is None:
        session.add(
            Partner(
                id="rds-smoke-alpha",
                name="RDS Smoke Alpha",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )
' >/dev/null

partner_event_id="RDS-SMOKE-${TRACKRELAY_SMOKE_SUFFIX}"
tracking_number="RDS-SMOKE-TRK-${TRACKRELAY_SMOKE_SUFFIX}"
smoke_response="$(
    curl \
        --fail-with-body \
        --silent \
        --show-error \
        --request POST \
        --header 'Content-Type: application/json' \
        --data "{\"event_id\":\"${partner_event_id}\",\"tracking_number\":\"${tracking_number}\",\"status\":\"PICKUP\",\"event_time\":\"2026-08-26T09:00:00+07:00\"}" \
        http://127.0.0.1:8000/api/v1/partners/rds-smoke-alpha/events
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

compose restart --timeout 10 api >/dev/null
wait_for_api_readiness
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

printf 'TRACKRELAY_RDS_READY\n'
