#!/usr/bin/env bash

set -euxo pipefail

dnf install --assumeyes docker
systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
usermod --append --groups docker ec2-user
