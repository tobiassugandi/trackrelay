#!/usr/bin/env bash

set -euxo pipefail

dnf install --assumeyes docker

compose_version="v2.32.4"
compose_sha256="ed1917fb54db184192ea9d0717bcd59e3662ea79db48bff36d3475516c480a6b"
compose_directory="/usr/local/lib/docker/cli-plugins"
compose_download="/tmp/docker-compose-linux-x86_64"

install --directory --mode 0755 "${compose_directory}"
curl \
    --fail \
    --location \
    --silent \
    --show-error \
    "https://github.com/docker/compose/releases/download/${compose_version}/docker-compose-linux-x86_64" \
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
