"""Tests for service-native TrackRelay teardown checks."""

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


def absent_resource_runner(
    arguments: Sequence[str],
) -> CompletedProcess[str]:
    call = tuple(arguments)
    if "describe-repositories" in call:
        return completed(
            call,
            stderr="RepositoryNotFoundException",
            returncode=254,
        )
    if "get-queue-url" in call:
        return completed(
            call,
            stderr="AWS.SimpleQueueService.NonExistentQueue",
            returncode=254,
        )
    if "describe-load-balancers" in call:
        return completed(call, stderr="LoadBalancerNotFound", returncode=254)
    if "describe-target-groups" in call:
        return completed(call, stderr="TargetGroupNotFound", returncode=254)
    if "list-services" in call:
        return completed(call, stderr="ClusterNotFoundException", returncode=254)
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


def test_inventory_proves_every_resource_type_absent() -> None:
    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=absent_resource_runner,
    )

    assert counts == {
        "active_ecs_task_definitions": 0,
        "application_load_balancers": 0,
        "cloudwatch_log_groups": 0,
        "ebs_volumes": 0,
        "ec2_instances": 0,
        "ecr_repositories": 0,
        "ecs_clusters": 0,
        "ecs_services": 0,
        "ecs_tasks": 0,
        "iam_instance_profiles": 0,
        "iam_roles": 0,
        "internet_gateways": 0,
        "load_balancer_target_groups": 0,
        "rds_automated_backups": 0,
        "rds_instances": 0,
        "rds_managed_secrets": 0,
        "rds_manual_snapshots": 0,
        "rds_parameter_groups": 0,
        "rds_subnet_groups": 0,
        "route_tables": 0,
        "security_groups": 0,
        "service_discovery_namespaces": 0,
        "sqs_queues": 0,
        "subnets": 0,
        "vpcs": 0,
    }


def test_inventory_counts_a_remaining_instance_without_persisting_its_id() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "describe-instances" in call:
            return completed(call, stdout="1\n")
        return absent_resource_runner(call)

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
        return absent_resource_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["rds_managed_secrets"] == 1
    assert sum(counts.values()) == 1


def test_inventory_counts_each_service_repository_and_delivery_queue() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "describe-repositories" in call and (
            "trackrelay-" in call[call.index("--repository-names") + 1]
            and call[call.index("--repository-names") + 1].endswith("-worker")
        ):
            return completed(call, stdout="{}\n")
        if "get-queue-url" in call and call[
            call.index("--queue-name") + 1
        ].endswith("-delivery"):
            return completed(call, stdout="{}\n")
        return absent_resource_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["ecr_repositories"] == 1
    assert counts["sqs_queues"] == 1
    assert sum(counts.values()) == 2


def test_inventory_counts_remaining_async_platform_resources() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "describe-load-balancers" in call:
            return completed(call, stdout="{}\n")
        if "describe-target-groups" in call:
            return completed(call, stdout="{}\n")
        if "describe-clusters" in call or "describe-log-groups" in call:
            return completed(call, stdout="1\n")
        if "get-role" in call and call[
            call.index("--role-name") + 1
        ].endswith("-worker-task"):
            return completed(call, stdout="{}\n")
        return absent_resource_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["application_load_balancers"] == 1
    assert counts["load_balancer_target_groups"] == 1
    assert counts["ecs_clusters"] == 1
    assert counts["cloudwatch_log_groups"] == 1
    assert counts["iam_roles"] == 1
    assert sum(counts.values()) == 5


def test_inventory_counts_remaining_async_runtime_resources() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if (
            "list-services" in call
            or "list-task-definitions" in call
            or "list-namespaces" in call
        ):
            return completed(call, stdout="1\n")
        return absent_resource_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["ecs_services"] == 1
    assert counts["active_ecs_task_definitions"] == 1
    assert counts["service_discovery_namespaces"] == 1
    assert sum(counts.values()) == 3


def test_inventory_counts_pending_and_running_ecs_tasks() -> None:
    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if "list-tasks" in call:
            desired_status = call[call.index("--desired-status") + 1]
            return completed(call, stdout="2\n" if desired_status == "RUNNING" else "1\n")
        return absent_resource_runner(call)

    counts = inventory_rehost_resources(
        profile=PROFILE,
        region=REGION,
        session_id=SESSION_ID,
        runner=runner,
    )

    assert counts["ecs_tasks"] == 3
    assert sum(counts.values()) == 3


def test_inventory_uses_explicit_profile_region_and_session_tags() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        return absent_resource_runner(call)

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

    repository_names = {
        call[call.index("--repository-names") + 1]
        for call in calls
        if "describe-repositories" in call
    }
    queue_names = {
        call[call.index("--queue-name") + 1]
        for call in calls
        if "get-queue-url" in call
    }
    role_names = {
        call[call.index("--role-name") + 1]
        for call in calls
        if "get-role" in call
    }
    assert {name.rsplit("-", 1)[-1] for name in repository_names} == {
        "api",
        "simulator",
        "worker",
    }
    assert {name.split("-delivery", 1)[-1] for name in queue_names} == {
        "",
        "-dlq",
    }
    assert {name.split("trackrelay-", 1)[-1].split("-", 1)[-1] for name in role_names} == {
        "api-task",
        "ecs-execution",
        "instance",
        "migration-task",
        "simulator-task",
        "worker-task",
    }

    load_balancer_call = next(
        call for call in calls if "describe-load-balancers" in call
    )
    target_group_call = next(
        call for call in calls if "describe-target-groups" in call
    )
    cluster_call = next(call for call in calls if "describe-clusters" in call)
    service_call = next(call for call in calls if "list-services" in call)
    task_definition_call = next(
        call for call in calls if "list-task-definitions" in call
    )
    namespace_call = next(call for call in calls if "list-namespaces" in call)
    log_group_call = next(
        call for call in calls if "describe-log-groups" in call
    )
    assert load_balancer_call[load_balancer_call.index("--names") + 1].endswith(
        "-async"
    )
    assert target_group_call[target_group_call.index("--names") + 1].endswith(
        "-api"
    )
    assert cluster_call[cluster_call.index("--clusters") + 1].endswith("-async")
    assert service_call[service_call.index("--cluster") + 1].endswith("-async")
    assert task_definition_call[
        task_definition_call.index("--family-prefix") + 1
    ].endswith("-")
    assert task_definition_call[
        task_definition_call.index("--status") + 1
    ] == "ACTIVE"
    assert "trackrelay-" in namespace_call[namespace_call.index("--query") + 1]
    assert log_group_call[
        log_group_call.index("--log-group-name-prefix") + 1
    ].startswith("/trackrelay/")

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
