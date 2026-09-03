"""Ordering contract for telemetry resources that AWS can create implicitly."""

from pathlib import Path
from re import search


def test_cluster_waits_for_terraform_owned_performance_group():
    source = (
        Path(__file__).resolve().parents[1] / "infra/terraform/async_platform.tf"
    ).read_text()
    cluster = source.split('resource "aws_ecs_cluster" "async" {', 1)[1].split(
        '\nresource "', 1
    )[0]
    assert search(
        r"depends_on\s*=\s*\[aws_cloudwatch_log_group\.async_performance\]",
        cluster,
    )
