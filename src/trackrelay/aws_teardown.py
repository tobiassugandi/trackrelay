"""Native AWS absence checks for every TrackRelay infrastructure resource."""

from collections.abc import Callable, Sequence
from hashlib import sha256
from subprocess import CompletedProcess

CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]


class AwsTeardownCheckError(RuntimeError):
    """A native AWS inventory check could not establish absence."""


def aws_command_prefix(*, profile: str, region: str) -> tuple[str, ...]:
    """Select AWS credentials and region without ambient configuration."""
    return (
        "aws",
        "--profile",
        profile,
        "--region",
        region,
    )


def count_query(
    *,
    name: str,
    command: Sequence[str],
    runner: CommandRunner,
) -> int:
    """Run one AWS CLI query that returns only a numeric count."""
    result = runner(command)
    if result.returncode != 0:
        raise AwsTeardownCheckError(f"native absence check failed: {name}")
    try:
        count = int(result.stdout.strip())
    except ValueError as error:
        raise AwsTeardownCheckError(
            f"native absence check returned an invalid count: {name}"
        ) from error
    if count < 0:
        raise AwsTeardownCheckError(
            f"native absence check returned an invalid count: {name}"
        )
    return count


def named_resource_exists(
    *,
    name: str,
    command: Sequence[str],
    not_found_marker: str,
    runner: CommandRunner,
) -> int:
    """Return zero only for an authoritative service-specific not-found error."""
    result = runner(command)
    if result.returncode == 0:
        return 1
    if not_found_marker in result.stderr:
        return 0
    raise AwsTeardownCheckError(f"native absence check failed: {name}")


def inventory_rehost_resources(
    *,
    profile: str,
    region: str,
    session_id: str,
    runner: CommandRunner,
) -> dict[str, int]:
    """Count every native resource type introduced through Stage 9.5."""
    prefix = aws_command_prefix(profile=profile, region=region)
    tag_filters = (
        "Name=tag:Project,Values=TrackRelay",
        f"Name=tag:SessionId,Values={session_id}",
    )
    active_instance_states = (
        "Name=instance-state-name,"
        "Values=pending,running,shutting-down,stopping,stopped"
    )
    count_commands = {
        "ec2_instances": (
            *prefix,
            "ec2",
            "describe-instances",
            "--filters",
            *tag_filters,
            active_instance_states,
            "--query",
            "length(Reservations[].Instances[])",
            "--output",
            "text",
        ),
        "ebs_volumes": (
            *prefix,
            "ec2",
            "describe-volumes",
            "--filters",
            *tag_filters,
            "--query",
            "length(Volumes)",
            "--output",
            "text",
        ),
        "internet_gateways": (
            *prefix,
            "ec2",
            "describe-internet-gateways",
            "--filters",
            *tag_filters,
            "--query",
            "length(InternetGateways)",
            "--output",
            "text",
        ),
        "route_tables": (
            *prefix,
            "ec2",
            "describe-route-tables",
            "--filters",
            *tag_filters,
            "--query",
            "length(RouteTables)",
            "--output",
            "text",
        ),
        "security_groups": (
            *prefix,
            "ec2",
            "describe-security-groups",
            "--filters",
            *tag_filters,
            "--query",
            "length(SecurityGroups)",
            "--output",
            "text",
        ),
        "subnets": (
            *prefix,
            "ec2",
            "describe-subnets",
            "--filters",
            *tag_filters,
            "--query",
            "length(Subnets)",
            "--output",
            "text",
        ),
        "vpcs": (
            *prefix,
            "ec2",
            "describe-vpcs",
            "--filters",
            *tag_filters,
            "--query",
            "length(Vpcs)",
            "--output",
            "text",
        ),
    }
    counts = {
        name: count_query(name=name, command=command, runner=runner)
        for name, command in count_commands.items()
    }

    resource_suffix = sha256(session_id.encode()).hexdigest()[:8]
    name_prefix = f"trackrelay-{resource_suffix}"
    database_identifier = f"{name_prefix}-postgres"
    counts["ecr_repositories"] = sum(
        named_resource_exists(
            name=f"ecr_repositories_{role}",
            command=(
                *prefix,
                "ecr",
                "describe-repositories",
                "--repository-names",
                f"{name_prefix}-{role}",
                "--output",
                "json",
            ),
            not_found_marker="RepositoryNotFoundException",
            runner=runner,
        )
        for role in ("api", "worker", "simulator")
    )
    counts["sqs_queues"] = sum(
        named_resource_exists(
            name=f"sqs_queues_{queue_suffix}",
            command=(
                *prefix,
                "sqs",
                "get-queue-url",
                "--queue-name",
                f"{name_prefix}-{queue_suffix}",
                "--output",
                "json",
            ),
            not_found_marker="AWS.SimpleQueueService.NonExistentQueue",
            runner=runner,
        )
        for queue_suffix in ("delivery", "delivery-dlq")
    )
    counts["iam_instance_profiles"] = named_resource_exists(
        name="iam_instance_profiles",
        command=(
            *prefix,
            "iam",
            "get-instance-profile",
            "--instance-profile-name",
            f"{name_prefix}-instance",
            "--output",
            "json",
        ),
        not_found_marker="NoSuchEntity",
        runner=runner,
    )
    counts["iam_roles"] = named_resource_exists(
        name="iam_roles",
        command=(
            *prefix,
            "iam",
            "get-role",
            "--role-name",
            f"{name_prefix}-instance",
            "--output",
            "json",
        ),
        not_found_marker="NoSuchEntity",
        runner=runner,
    )
    counts["rds_instances"] = named_resource_exists(
        name="rds_instances",
        command=(
            *prefix,
            "rds",
            "describe-db-instances",
            "--db-instance-identifier",
            database_identifier,
            "--output",
            "json",
        ),
        not_found_marker="DBInstanceNotFound",
        runner=runner,
    )
    counts["rds_automated_backups"] = named_resource_exists(
        name="rds_automated_backups",
        command=(
            *prefix,
            "rds",
            "describe-db-instance-automated-backups",
            "--db-instance-identifier",
            database_identifier,
            "--output",
            "json",
        ),
        not_found_marker="DBInstanceAutomatedBackupNotFound",
        runner=runner,
    )
    counts["rds_manual_snapshots"] = count_query(
        name="rds_manual_snapshots",
        command=(
            *prefix,
            "rds",
            "describe-db-snapshots",
            "--db-instance-identifier",
            database_identifier,
            "--snapshot-type",
            "manual",
            "--query",
            "length(DBSnapshots)",
            "--output",
            "text",
        ),
        runner=runner,
    )
    counts["rds_subnet_groups"] = named_resource_exists(
        name="rds_subnet_groups",
        command=(
            *prefix,
            "rds",
            "describe-db-subnet-groups",
            "--db-subnet-group-name",
            database_identifier,
            "--output",
            "json",
        ),
        not_found_marker="DBSubnetGroupNotFoundFault",
        runner=runner,
    )
    counts["rds_parameter_groups"] = named_resource_exists(
        name="rds_parameter_groups",
        command=(
            *prefix,
            "rds",
            "describe-db-parameter-groups",
            "--db-parameter-group-name",
            database_identifier,
            "--output",
            "json",
        ),
        not_found_marker="DBParameterGroupNotFound",
        runner=runner,
    )
    secret_tag_query = (
        "length(SecretList[?length(Tags[?"
        "(Key=='aws:rds:primarydbinstancearn' || "
        "Key=='aws:rds:primaryDBInstanceArn') && "
        f"ends_with(Value, ':db:{database_identifier}')]) > `0`])"
    )
    counts["rds_managed_secrets"] = count_query(
        name="rds_managed_secrets",
        command=(
            *prefix,
            "secretsmanager",
            "list-secrets",
            "--include-planned-deletion",
            "--filters",
            "Key=owning-service,Values=rds",
            "--query",
            secret_tag_query,
            "--output",
            "text",
        ),
        runner=runner,
    )
    return counts
