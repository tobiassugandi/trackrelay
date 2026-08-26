#!/usr/bin/env bash

set -euxo pipefail

dnf install --assumeyes docker

compose_version="v2.32.4"
compose_sha256="0c4591cf3b1ed039adcd803dbbeddf757375fc08c11245b0154135f838495a2f"
compose_directory="/usr/local/lib/docker/cli-plugins"
compose_download="/tmp/docker-compose-linux-aarch64"

install --directory --mode 0755 "${compose_directory}"
curl \
    --fail \
    --location \
    --silent \
    --show-error \
    "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-aarch64" \
    --output "${compose_download}"
printf '%s  %s\n' "${compose_sha256}" "${compose_download}" | sha256sum --check
install \
    --mode 0755 \
    "${compose_download}" \
    "${compose_directory}/docker-compose"
rm --force "${compose_download}"

systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
usermod --append --groups docker ec2-user
docker compose version
