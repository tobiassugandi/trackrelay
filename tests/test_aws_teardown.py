"""Tests for service-native synchronous-rehost teardown checks."""

from collections.abc import Sequence
from subprocess import CompletedProcess

from pytest import raises

from trackrelay.aws_teardown import (
    AwsTeardownCheckError,
    inventory_rehost_resources,
)

PROFILE = "trackrelay-admin"
REGION = "ap-southeast-3"
SESSION_ID = "cloud-session-1-20260824T090000Z"


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, stderr)


def absent_rehost_runner(
    arguments: Sequence[str],
) -> CompletedProcess[str]:
    call = tuple(arguments)
    if "describe-repositories" in call:
        return completed(
            call,
            stderr="RepositoryNotFoundException",
            returncode=254,
        )
    if "get-instance-profile" in call or "get-role" in call:
        return completed(call, stderr="NoSuchEntity", returncode=254)
    not_found_by_command = {
        "describe-db-instances": "DBInstanceNotFound",
        "describe-db-instance-automated-backups": (
            "DBInstanceAutomatedBackupNotFound"
        ),
        "describe-db-subnet-groups": "DBSubnetGroupNotFoundFault",
        "describe-db-parameter-groups": "DBParameterGroupNotFound",
    }
    for command, marker in not_found_by_command.items():
        if command in call:
            return completed(call, stderr=marker, returncode=254)
    return completed(call, stdout="0\n")


def test_inventory_proves_every_rehost_resource_type_absent() -> None:
    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=absent_rehost_runner,
    )

    assert counts == {
        "ebs_volumes": 0,
        "ec2_instances": 0,
        "ecr_repositories": 0,
        "iam_instance_profiles": 0,
        "iam_roles": 0,
        "internet_gateways": 0,
        "rds_automated_backups": 0,
        "rds_instances": 0,
        "rds_managed_secrets": 0,
        "rds_manual_snapshots": 0,
        "rds_parameter_groups": 0,
        "rds_subnet_groups": 0,
        "route_tables": 0,
        "security_groups": 0,
        "subnets": 0,
        "vpcs": 0,
    }


def test_inventory_counts_a_remaining_instance_without_persisting_its_id() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "describe-instances" in call:
            return completed(call, stdout="1\n")
        return absent_rehost_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["ec2_instances"] == 1
    assert sum(counts.values()) == 1


def test_inventory_counts_an_rds_managed_secret_pending_deletion() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "list-secrets" in call:
            return completed(call, stdout="1\n")
        return absent_rehost_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["rds_managed_secrets"] == 1
    assert sum(counts.values()) == 1


def test_inventory_uses_explicit_profile_region_and_session_tags() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        return absent_rehost_runner(call)

    inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    for call in calls:
        assert call[:5] == (
            "aws",
            "--profile",
            PROFILE,
            "--region",
            REGION,
        )
    tag_filtered_calls = [
        call for call in calls if call[5] == "ec2" and "--filters" in call
    ]
    assert tag_filtered_calls
    for call in tag_filtered_calls:
        assert "Name=tag:Project,Values=TrackRelay" in call
        assert f"Name=tag:SessionId,Values={SESSION_ID}" in call

    secret_call = next(call for call in calls if "list-secrets" in call)
    assert "--include-planned-deletion" in secret_call
    assert "Key=owning-service,Values=rds" in secret_call
    secret_query = secret_call[secret_call.index("--query") + 1]
    assert "aws:rds:primarydbinstancearn" in secret_query
    assert "trackrelay-" in secret_query


def test_inventory_does_not_treat_an_authentication_error_as_absence() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        return completed(arguments, stderr="session expired", returncode=255)

    with raises(AwsTeardownCheckError, match="native absence check failed"):
        inventory_rehost_resources(
            profile=PROFILE,
            region=REGION,
            session_id=SESSION_ID,
            runner=runner,
        )
