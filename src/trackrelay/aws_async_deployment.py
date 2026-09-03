"""Deploy TrackRelay's fixed asynchronous AWS runtime in guarded phases."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from re import compile as compile_pattern
from signal import SIGTERM, getsignal, signal
from subprocess import CompletedProcess, run
from typing import NoReturn
from urllib.parse import urlsplit

from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    destroy_session,
    file_sha256,
    load_manifest,
    parse_positive_money,
    session_from_arguments,
    verify_destroyed,
    write_command_log,
    write_manifest,
)
from trackrelay.operator_status import (
    operator_failure,
    operator_status,
    status_activity,
)

ProcessRunner = Callable[
    [Sequence[str], str | None],
    CompletedProcess[str],
]
SessionAction = Callable[..., object]
ImagePublisher = Callable[..., dict[str, str]]

IMAGE_ROLES = ("api", "worker", "simulator")
IMAGE_OUTPUTS = {
    "api": "rehost_ecr_repository_url",
    "worker": "worker_ecr_repository_url",
    "simulator": "simulator_ecr_repository_url",
}
IMAGE_DIGEST_PATTERN = compile_pattern(r"^sha256:[0-9a-f]{64}$")
CLUSTER_NAME_PATTERN = compile_pattern(r"^trackrelay-[0-9a-f]{8}-async$")
SECURITY_GROUP_ID_PATTERN = compile_pattern(r"^sg-[0-9a-f]{8,17}$")
SUBNET_ID_PATTERN = compile_pattern(r"^subnet-[0-9a-f]{8,17}$")
TASK_DEFINITION_ARN_PATTERN = compile_pattern(
    r"^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/"
    r"trackrelay-[0-9a-f]{8}-migration:[1-9][0-9]*$"
)
TASK_ARN_PATTERN = compile_pattern(r"^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[^\s]+$")
SERVICE_NAME_PATTERN = compile_pattern(
    r"^trackrelay-[0-9a-f]{8}-(api|simulator|worker)$"
)
FIXED_SERVICE_CAPACITY = {
    "api": {"cpu_units": 1024, "desired_count": 2, "memory_mib": 2048},
    "simulator": {"cpu_units": 256, "desired_count": 1, "memory_mib": 512},
    "worker": {"cpu_units": 256, "desired_count": 1, "memory_mib": 512},
}


class AwsAsyncDeploymentError(RuntimeError):
    """A safe, actionable asynchronous deployment failure."""


class AwsAsyncDeploymentCleanupError(RuntimeError):
    """Report cleanup failures without hiding the deployment failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException,
        cleanup_errors: Sequence[BaseException],
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


def run_process(
    arguments: Sequence[str],
    input_text: str | None = None,
) -> CompletedProcess[str]:
    """Run without streaming cloud identifiers or temporary credentials."""
    return run(
        arguments,
        capture_output=True,
        check=False,
        input=input_text,
        text=True,
    )


def invoke(
    runner: ProcessRunner,
    arguments: Sequence[str],
    *,
    action: str,
    input_text: str | None = None,
) -> CompletedProcess[str]:
    """Run one command and expose no captured output on failure."""
    result = runner(arguments, input_text)
    if result.returncode != 0:
        raise AwsAsyncDeploymentError(f"{action} failed")
    return result


def aws_prefix(session: AwsSession) -> tuple[str, ...]:
    """Select the approved profile and region explicitly."""
    return (
        "aws",
        "--profile",
        session.profile,
        "--region",
        session.region,
    )


def _recorded_cost_ceiling(manifest: dict[str, object]) -> Decimal:
    value = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(value, str):
        raise AwsSessionError("the applied session has no approved cost ceiling")
    return parse_positive_money(value, field_name="recorded cost ceiling")


def validate_async_deployment_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> dict[str, object]:
    """Require an exact async-session approval before any workflow side effect."""
    if not session.session_id.startswith(("cloud-session-3-", "cloud-session-4-")):
        raise AwsSessionError("asynchronous deployment must use cloud session 3 or 4")
    if session.deployment_mode != "async":
        raise AwsSessionError("asynchronous deployment requires async mode")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    approved_ceiling = parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    )
    manifest = load_manifest(session)
    if manifest.get("status") != "applied":
        raise AwsSessionError(
            "asynchronous deployment must start immediately after foundation apply"
        )
    if approved_ceiling != _recorded_cost_ceiling(manifest):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    return manifest


def require_clean_approved_revision(
    session: AwsSession,
    *,
    runner: ProcessRunner,
) -> tuple[dict[str, object], str]:
    """Bind every artifact and phase to the initially approved revision."""
    manifest = load_manifest(session)
    status = invoke(
        runner,
        ("git", "status", "--porcelain"),
        action="Git worktree inspection",
    )
    if status.stdout.strip():
        raise AwsAsyncDeploymentError("Git worktree must be clean")
    revision = invoke(
        runner,
        ("git", "rev-parse", "HEAD"),
        action="Git revision inspection",
    ).stdout.strip()
    if not revision or manifest.get("git_revision") != revision:
        raise AwsAsyncDeploymentError("Git revision differs from the approved plan")
    return manifest, revision


def terraform_output(
    session: AwsSession,
    name: str,
    *,
    runner: ProcessRunner,
    json_output: bool = False,
) -> object:
    """Read one Terraform output in memory without printing it."""
    arguments = session.terraform_command(
        "output",
        "-json" if json_output else "-raw",
        name,
    )
    value = invoke(
        runner,
        arguments,
        action=f"Terraform output {name}",
    ).stdout.strip()
    if not value:
        raise AwsAsyncDeploymentError(f"Terraform output {name} is empty")
    if not json_output:
        return value
    try:
        return loads(value)
    except JSONDecodeError as error:
        raise AwsAsyncDeploymentError(
            f"Terraform output {name} is invalid JSON"
        ) from error


def validate_repository_url(repository_url: str, *, region: str) -> None:
    """Accept only a private ECR repository URL in the selected region."""
    parsed = urlsplit(f"https://{repository_url}")
    expected_host_suffix = f".dkr.ecr.{region}.amazonaws.com"
    if (
        parsed.hostname is None
        or not parsed.hostname.endswith(expected_host_suffix)
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise AwsAsyncDeploymentError(
            "Terraform returned an invalid ECR repository URL"
        )


def publish_async_images(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: datetime | None = None,
) -> dict[str, str]:
    """Publish all three AMD64 targets and retain only immutable digests."""
    manifest, revision = require_clean_approved_revision(session, runner=runner)
    if manifest.get("status") != "async_deployment_armed":
        raise AwsAsyncDeploymentError(
            "image publication requires an armed async deployment"
        )
    repositories: dict[str, str] = {}
    for role, output_name in IMAGE_OUTPUTS.items():
        repository_url = terraform_output(
            session,
            output_name,
            runner=runner,
        )
        if not isinstance(repository_url, str):
            raise AwsAsyncDeploymentError(f"Terraform output {output_name} is invalid")
        validate_repository_url(repository_url, region=session.region)
        repositories[role] = repository_url
    registry_hosts = {url.split("/", maxsplit=1)[0] for url in repositories.values()}
    if len(registry_hosts) != 1:
        raise AwsAsyncDeploymentError("service repositories use different registries")
    registry_host = next(iter(registry_hosts))
    image_tag = f"git-{revision[:12]}"

    password = invoke(
        runner,
        (*aws_prefix(session), "ecr", "get-login-password"),
        action="ECR authentication",
    ).stdout
    if not password:
        raise AwsAsyncDeploymentError("ECR returned an empty login password")
    invoke(
        runner,
        (
            "docker",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            registry_host,
        ),
        input_text=password,
        action="Docker ECR login",
    )
    digests: dict[str, str] = {}
    try:
        for role in IMAGE_ROLES:
            repository_url = repositories[role]
            invoke(
                runner,
                (
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--provenance=false",
                    "--sbom=false",
                    "--file",
                    "Dockerfile",
                    "--target",
                    role,
                    "--tag",
                    f"{repository_url}:{image_tag}",
                    "--push",
                    ".",
                ),
                action=f"{role} AMD64 image publication",
            )
            repository_name = repository_url.split("/", maxsplit=1)[1]
            digest = invoke(
                runner,
                (
                    *aws_prefix(session),
                    "ecr",
                    "describe-images",
                    "--repository-name",
                    repository_name,
                    "--image-ids",
                    f"imageTag={image_tag}",
                    "--query",
                    "imageDetails[0].imageDigest",
                    "--output",
                    "text",
                ),
                action=f"{role} ECR image digest lookup",
            ).stdout.strip()
            if IMAGE_DIGEST_PATTERN.fullmatch(digest) is None:
                raise AwsAsyncDeploymentError(
                    f"ECR returned an invalid {role} image digest"
                )
            digests[role] = digest
    finally:
        runner(("docker", "logout", registry_host), None)

    published_at = now or datetime.now(UTC)
    manifest.update(
        {
            "async_images": {
                role: {
                    "architecture": "linux/amd64",
                    "digest": digests[role],
                    "tag": image_tag,
                }
                for role in IMAGE_ROLES
            },
            "async_images_published_at": published_at.isoformat(),
            "status": "async_images_published",
        }
    )
    write_manifest(session, manifest)
    return digests


def _validate_image_digests(image_digests: dict[str, str]) -> None:
    if set(image_digests) != set(IMAGE_ROLES) or not all(
        isinstance(digest, str) and IMAGE_DIGEST_PATTERN.fullmatch(digest) is not None
        for digest in image_digests.values()
    ):
        raise AwsAsyncDeploymentError("all three immutable image digests are required")


def _async_phase_variables(
    session: AwsSession,
    *,
    image_digests: dict[str, str],
    services_enabled: bool,
) -> tuple[str, ...]:
    _validate_image_digests(image_digests)
    return (
        *session.terraform_variables(),
        f"-var=api_image_digest={image_digests['api']}",
        f"-var=worker_image_digest={image_digests['worker']}",
        f"-var=simulator_image_digest={image_digests['simulator']}",
        f"-var=async_services_enabled={'true' if services_enabled else 'false'}",
    )


def apply_async_phase(
    session: AwsSession,
    *,
    phase: str,
    image_digests: dict[str, str],
    services_enabled: bool,
    runner: ProcessRunner = run_process,
    now: datetime | None = None,
) -> None:
    """Plan, journal, and apply one exact immutable async Terraform phase."""
    expected_services = phase == "services"
    if phase not in {"runtime", "services"} or services_enabled != expected_services:
        raise AwsAsyncDeploymentError("invalid asynchronous Terraform phase")
    manifest, _revision = require_clean_approved_revision(session, runner=runner)
    expected_status = (
        "async_images_published" if phase == "runtime" else "async_migration_succeeded"
    )
    if manifest.get("status") != expected_status:
        raise AwsAsyncDeploymentError(
            f"{phase} phase cannot follow status {manifest.get('status')!r}"
        )
    plan_path = session.evidence_dir / f"terraform-async-{phase}.tfplan"
    if plan_path.exists():
        raise AwsAsyncDeploymentError(f"saved {phase} phase plan already exists")
    plan_result = runner(
        session.terraform_command(
            "plan",
            "-input=false",
            f"-out={plan_path.resolve()}",
            *_async_phase_variables(
                session,
                image_digests=image_digests,
                services_enabled=services_enabled,
            ),
        ),
        None,
    )
    plan_log = session.evidence_dir / f"terraform-async-{phase}-plan.log"
    write_command_log(plan_log, plan_result)
    if plan_result.returncode != 0:
        raise AwsAsyncDeploymentError(f"Terraform {phase} plan failed")
    if not plan_path.is_file():
        raise AwsAsyncDeploymentError(
            f"Terraform did not create the {phase} phase plan"
        )

    phase_plans = manifest.setdefault("async_phase_plans", {})
    if not isinstance(phase_plans, dict):
        raise AwsAsyncDeploymentError("async phase-plan evidence is invalid")
    planned_at = now or datetime.now(UTC)
    phase_plans[phase] = {
        "plan_sha256": file_sha256(plan_path),
        "planned_at": planned_at.isoformat(),
        "services_enabled": services_enabled,
    }
    manifest["status"] = f"async_{phase}_planned"
    write_manifest(session, manifest)
    if phase_plans[phase]["plan_sha256"] != file_sha256(plan_path):
        raise AwsAsyncDeploymentError(
            f"saved {phase} phase plan changed after planning"
        )

    apply_result = runner(
        session.terraform_command(
            "apply",
            "-input=false",
            plan_path.resolve().as_posix(),
        ),
        None,
    )
    apply_log = session.evidence_dir / f"terraform-async-{phase}-apply.log"
    write_command_log(apply_log, apply_result)
    if apply_result.returncode != 0:
        raise AwsAsyncDeploymentError(f"Terraform {phase} apply failed")
    manifest["status"] = f"async_{phase}_applied"
    phase_plans[phase]["applied_at"] = planned_at.isoformat()
    write_manifest(session, manifest)


def _migration_configuration(
    session: AwsSession,
    *,
    runner: ProcessRunner,
) -> tuple[str, list[str], list[str], str]:
    value = terraform_output(
        session,
        "async_migration_run_configuration",
        runner=runner,
        json_output=True,
    )
    if not isinstance(value, dict):
        raise AwsAsyncDeploymentError("migration run configuration is invalid")
    cluster_name = value.get("cluster_name")
    security_group_ids = value.get("security_group_ids")
    subnet_ids = value.get("subnet_ids")
    task_definition_arn = value.get("task_definition_arn")
    if (
        not isinstance(cluster_name, str)
        or CLUSTER_NAME_PATTERN.fullmatch(cluster_name) is None
        or not isinstance(security_group_ids, list)
        or len(security_group_ids) != 1
        or not all(
            isinstance(item, str)
            and SECURITY_GROUP_ID_PATTERN.fullmatch(item) is not None
            for item in security_group_ids
        )
        or not isinstance(subnet_ids, list)
        or len(subnet_ids) != 2
        or not all(
            isinstance(item, str) and SUBNET_ID_PATTERN.fullmatch(item) is not None
            for item in subnet_ids
        )
        or not isinstance(task_definition_arn, str)
        or TASK_DEFINITION_ARN_PATTERN.fullmatch(task_definition_arn) is None
    ):
        raise AwsAsyncDeploymentError("migration run configuration is invalid")
    return cluster_name, security_group_ids, subnet_ids, task_definition_arn


def run_async_migration(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: datetime | None = None,
) -> None:
    """Run the one-off migration task and require its exact zero exit."""
    manifest, _revision = require_clean_approved_revision(session, runner=runner)
    if manifest.get("status") != "async_runtime_applied":
        raise AwsAsyncDeploymentError("migration requires the applied runtime phase")
    cluster_name, security_group_ids, subnet_ids, task_definition_arn = (
        _migration_configuration(session, runner=runner)
    )
    network_configuration = dumps(
        {
            "awsvpcConfiguration": {
                "assignPublicIp": "ENABLED",
                "securityGroups": security_group_ids,
                "subnets": subnet_ids,
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    task_arn = invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "run-task",
            "--cluster",
            cluster_name,
            "--task-definition",
            task_definition_arn,
            "--launch-type",
            "FARGATE",
            "--platform-version",
            "1.4.0",
            "--count",
            "1",
            "--network-configuration",
            network_configuration,
            "--query",
            "tasks[0].taskArn",
            "--output",
            "text",
        ),
        action="ECS migration task start",
    ).stdout.strip()
    if TASK_ARN_PATTERN.fullmatch(task_arn) is None:
        raise AwsAsyncDeploymentError("ECS did not start exactly one migration task")
    manifest["status"] = "async_migration_running"
    write_manifest(session, manifest)
    invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "wait",
            "tasks-stopped",
            "--cluster",
            cluster_name,
            "--tasks",
            task_arn,
        ),
        action="ECS migration task completion",
    )
    result_text = invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "describe-tasks",
            "--cluster",
            cluster_name,
            "--tasks",
            task_arn,
            "--query",
            (
                "tasks[0].{last_status:lastStatus,stop_code:stopCode,"
                "container_name:containers[0].name,"
                "exit_code:containers[0].exitCode}"
            ),
            "--output",
            "json",
        ),
        action="ECS migration task result",
    ).stdout
    try:
        result = loads(result_text)
    except JSONDecodeError as error:
        raise AwsAsyncDeploymentError(
            "ECS returned an invalid migration result"
        ) from error
    if (
        not isinstance(result, dict)
        or result.get("last_status") != "STOPPED"
        or result.get("container_name") != "migration"
        or not isinstance(result.get("stop_code"), str)
        or not isinstance(result.get("exit_code"), int)
    ):
        raise AwsAsyncDeploymentError("ECS returned an invalid migration result")
    migration_result = {
        "container_name": result["container_name"],
        "exit_code": result["exit_code"],
        "last_status": result["last_status"],
        "stop_code": result["stop_code"],
    }
    (session.evidence_dir / "ecs-migration-result.json").write_text(
        dumps(migration_result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if result["exit_code"] != 0:
        raise AwsAsyncDeploymentError(
            "ECS migration task did not exit successfully "
            f"(exit code {result['exit_code']}, stop code {result['stop_code']})"
        )
    completed_at = now or datetime.now(UTC)
    manifest.update(
        {
            "async_migration": {
                "completed_at": completed_at.isoformat(),
                "exit_code": 0,
                "status": "succeeded",
            },
            "status": "async_migration_succeeded",
        }
    )
    write_manifest(session, manifest)


def wait_for_async_services(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: datetime | None = None,
) -> None:
    """Require every service to converge to its exact fixed capacity."""
    manifest, _revision = require_clean_approved_revision(session, runner=runner)
    if manifest.get("status") != "async_services_applied":
        raise AwsAsyncDeploymentError("service convergence requires services apply")
    cluster_name = terraform_output(
        session,
        "async_ecs_cluster_name",
        runner=runner,
    )
    service_names = terraform_output(
        session,
        "async_service_names",
        runner=runner,
        json_output=True,
    )
    service_capacity = terraform_output(
        session,
        "async_service_capacity",
        runner=runner,
        json_output=True,
    )
    if (
        not isinstance(cluster_name, str)
        or CLUSTER_NAME_PATTERN.fullmatch(cluster_name) is None
        or not isinstance(service_names, list)
        or len(service_names) != 3
        or not all(
            isinstance(name, str) and SERVICE_NAME_PATTERN.fullmatch(name) is not None
            for name in service_names
        )
        or len(set(service_names)) != 3
    ):
        raise AwsAsyncDeploymentError("Terraform returned invalid service identities")
    if not isinstance(service_capacity, dict) or set(service_capacity) != set(
        FIXED_SERVICE_CAPACITY
    ):
        raise AwsAsyncDeploymentError("Terraform returned invalid fixed capacity")
    normalized_capacity: dict[str, dict[str, int | str]] = {}
    for role, expected_capacity in FIXED_SERVICE_CAPACITY.items():
        observed = service_capacity.get(role)
        if not isinstance(observed, dict) or set(observed) != {
            "cpu_units",
            "desired_count",
            "memory_mib",
            "service_name",
        }:
            raise AwsAsyncDeploymentError("Terraform returned invalid fixed capacity")
        service_name = observed.get("service_name")
        numeric_capacity = {
            key: observed.get(key)
            for key in ("cpu_units", "desired_count", "memory_mib")
        }
        if (
            not isinstance(service_name, str)
            or SERVICE_NAME_PATTERN.fullmatch(service_name) is None
            or not service_name.endswith(f"-{role}")
            or any(type(value) is not int for value in numeric_capacity.values())
            or numeric_capacity != expected_capacity
        ):
            raise AwsAsyncDeploymentError("Terraform returned invalid fixed capacity")
        normalized_capacity[role] = {
            **expected_capacity,
            "service_name": service_name,
        }
    if {item["service_name"] for item in normalized_capacity.values()} != set(
        service_names
    ):
        raise AwsAsyncDeploymentError("Terraform returned inconsistent fixed services")
    invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "wait",
            "services-stable",
            "--cluster",
            cluster_name,
            "--services",
            *service_names,
        ),
        action="ECS fixed-service convergence",
    )
    description_text = invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "describe-services",
            "--cluster",
            cluster_name,
            "--services",
            *service_names,
            "--query",
            "services[].[serviceName,status,desiredCount,runningCount,pendingCount,length(deployments)]",
            "--output",
            "json",
        ),
        action="ECS fixed-service status",
    ).stdout
    try:
        descriptions = loads(description_text)
    except JSONDecodeError as error:
        raise AwsAsyncDeploymentError("ECS returned invalid service status") from error
    expected = sorted(
        [
            [
                capacity["service_name"],
                "ACTIVE",
                capacity["desired_count"],
                capacity["desired_count"],
                0,
                1,
            ]
            for capacity in normalized_capacity.values()
        ]
    )
    valid_descriptions = isinstance(descriptions, list) and all(
        isinstance(description, list)
        and len(description) == 6
        and isinstance(description[0], str)
        for description in descriptions
    )
    if not valid_descriptions or sorted(descriptions) != expected:
        raise AwsAsyncDeploymentError(
            "fixed services did not converge to their approved capacity"
        )
    operator_status(
        "ECS converged: API 2/2, worker 1/1, simulator 1/1; no pending tasks"
    )
    converged_at = now or datetime.now(UTC)
    manifest.update(
        {
            "async_fixed_services": normalized_capacity,
            "async_services_converged_at": converged_at.isoformat(),
            "status": "async_deployed",
        }
    )
    write_manifest(session, manifest)


def stop_async_migration_tasks(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
) -> None:
    """Stop any session migration task before Terraform removes its cluster."""
    resource_suffix = sha256(session.session_id.encode()).hexdigest()[:8]
    cluster_name = f"trackrelay-{resource_suffix}-async"
    family = f"trackrelay-{resource_suffix}-migration"
    task_arns: list[str] = []
    for desired_status in ("PENDING", "RUNNING"):
        result_text = invoke(
            runner,
            (
                *aws_prefix(session),
                "ecs",
                "list-tasks",
                "--cluster",
                cluster_name,
                "--family",
                family,
                "--desired-status",
                desired_status,
                "--query",
                "taskArns",
                "--output",
                "json",
            ),
            action="ECS migration cleanup inventory",
        ).stdout
        try:
            tasks = loads(result_text)
        except JSONDecodeError as error:
            raise AwsAsyncDeploymentError(
                "ECS returned an invalid migration cleanup inventory"
            ) from error
        if not isinstance(tasks, list) or not all(
            isinstance(task_arn, str)
            and TASK_ARN_PATTERN.fullmatch(task_arn) is not None
            for task_arn in tasks
        ):
            raise AwsAsyncDeploymentError(
                "ECS returned an invalid migration cleanup inventory"
            )
        task_arns.extend(tasks)
    unique_task_arns = sorted(set(task_arns))
    for task_arn in unique_task_arns:
        invoke(
            runner,
            (
                *aws_prefix(session),
                "ecs",
                "stop-task",
                "--cluster",
                cluster_name,
                "--task",
                task_arn,
                "--reason",
                "TrackRelay failure-safe deployment cleanup",
                "--query",
                "task.taskArn",
                "--output",
                "text",
            ),
            action="ECS migration task cleanup",
        )
    if unique_task_arns:
        invoke(
            runner,
            (
                *aws_prefix(session),
                "ecs",
                "wait",
                "tasks-stopped",
                "--cluster",
                cluster_name,
                "--tasks",
                *unique_task_arns,
            ),
            action="ECS migration task cleanup completion",
        )


def _arm_async_deployment(
    session: AwsSession,
    manifest: dict[str, object],
) -> None:
    manifest.update(
        {
            "async_deployment": {
                "deployment_order": [
                    "publish-images",
                    "register-runtime",
                    "run-migration",
                    "enable-fixed-services",
                    "verify-convergence",
                ],
                "fixed_service_capacity": FIXED_SERVICE_CAPACITY,
                "unconditional_teardown_armed": True,
            },
            "status": "async_deployment_armed",
        }
    )
    write_manifest(session, manifest)


def deploy_async_stack(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    publisher: ImagePublisher = publish_async_images,
    phase_applier: SessionAction = apply_async_phase,
    migrator: SessionAction = run_async_migration,
    convergence_waiter: SessionAction = wait_for_async_services,
    migration_task_stopper: SessionAction = stop_async_migration_tasks,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
) -> None:
    """Deploy in order and clean up automatically after any failure."""
    manifest = validate_async_deployment_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    workflow_error: BaseException | None = None
    try:
        _arm_async_deployment(session, manifest)
        with status_activity("Publishing immutable API, worker and simulator images"):
            image_digests = publisher(session)
        with status_activity("Applying runtime infrastructure"):
            phase_applier(
                session,
                phase="runtime",
                image_digests=image_digests,
                services_enabled=False,
            )
        with status_activity("Running database migration"):
            migrator(session)
        with status_activity("API, worker and simulator service startup"):
            phase_applier(
                session,
                phase="services",
                image_digests=image_digests,
                services_enabled=True,
            )
        with status_activity("Waiting for ECS fixed-service convergence"):
            convergence_waiter(session)
    except BaseException as error:  # noqa: BLE001 - teardown must follow interrupts
        workflow_error = error
        operator_failure("Phase deployment; beginning cleanup", error)

    if workflow_error is None:
        return

    preparation_errors: list[BaseException] = []
    try:
        with status_activity("Cleanup: stopping migration tasks"):
            migration_task_stopper(session)
    except BaseException as error:  # noqa: BLE001 - still destroy and verify
        preparation_errors.append(error)
    cleanup_errors: list[BaseException] = []
    try:
        destroyer(session)
    except BaseException as error:  # noqa: BLE001 - still verify natively
        cleanup_errors.append(error)
    try:
        teardown_verifier(session)
    except BaseException as error:  # noqa: BLE001 - report every cleanup failure
        cleanup_errors.append(error)
    if cleanup_errors:
        cleanup_errors = [*preparation_errors, *cleanup_errors]
        message = (
            "asynchronous deployment failed with "
            f"{type(workflow_error).__name__}: {workflow_error}; cleanup failed: "
            + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)
        )
        raise AwsAsyncDeploymentCleanupError(
            message,
            workflow_error=workflow_error,
            cleanup_errors=cleanup_errors,
        ) from workflow_error
    raise workflow_error


def build_parser() -> ArgumentParser:
    """Build the explicitly armed asynchronous deployment command."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument(
        "--approved-unconditional-teardown-session-id",
        required=True,
    )
    return parser


def run_from_arguments(arguments: Namespace) -> None:
    """Build the shared session object and execute the armed workflow."""
    deploy_async_stack(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    """Translate SIGTERM into an exception so the cleanup path executes."""
    raise KeyboardInterrupt("received SIGTERM during asynchronous deployment")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the armed workflow and translate ordinary failures concisely."""
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsAsyncDeploymentCleanupError,
        AwsAsyncDeploymentError,
        AwsSessionError,
        KeyboardInterrupt,
    ) as error:
        raise SystemExit(f"AWS async deployment failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    print("deployed the fixed asynchronous stack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
