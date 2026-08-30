"""Publish and deploy TrackRelay's synchronous AWS rehost without SSH."""

from argparse import ArgumentParser, Namespace
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from pathlib import Path
from re import compile as compile_pattern
from subprocess import CompletedProcess, run
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.parse import urlsplit

from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    load_manifest,
    session_from_arguments,
    write_manifest,
)

ProcessRunner = Callable[
    [Sequence[str], str | None],
    CompletedProcess[str],
]
Sleeper = Callable[[float], None]
IMAGE_DIGEST_PATTERN = compile_pattern(r"^sha256:[0-9a-f]{64}$")
INSTANCE_ID_PATTERN = compile_pattern(r"^i-[0-9a-f]{8,17}$")
COMMAND_ID_PATTERN = compile_pattern(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:"
    "18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
)
RDS_IDENTIFIER_PATTERN = compile_pattern(
    r"^trackrelay-[0-9a-f]{8}-postgres$"
)
RDS_ENGINE_VERSION_PATTERN = compile_pattern(r"^17\.[0-9]+$")


class AwsRehostError(RuntimeError):
    """A safe, actionable rehost publication or deployment failure."""


@dataclass(frozen=True)
class RehostFiles:
    """Committed runtime files transferred through SSM Run Command."""

    compose: Path
    installer: Path

    def validate(self) -> None:
        for path in (self.compose, self.installer):
            if not path.is_file():
                raise AwsRehostError(f"required rehost file is missing: {path}")


def run_process(
    arguments: Sequence[str],
    input_text: str | None = None,
) -> CompletedProcess[str]:
    """Run without streaming repository URLs, instance IDs, or credentials."""
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
    input_text: str | None = None,
    action: str,
) -> CompletedProcess[str]:
    """Run one command and expose no captured output when it fails."""
    result = runner(arguments, input_text)
    if result.returncode != 0:
        raise AwsRehostError(f"{action} failed")
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


def require_applied_clean_revision(
    session: AwsSession,
    *,
    runner: ProcessRunner,
) -> tuple[dict[str, object], str]:
    """Bind publication and deployment to the applied, committed revision."""
    manifest = load_manifest(session)
    if manifest.get("status") not in {
        "applied",
        "image_published",
        "rehost_deployed",
        "rehost_workload_collected",
        "rds_deployed",
        "rds_correctness_collected",
        "vertical_scaling_ready",
        "vertical_scaling_tier_collected",
        "vertical_scaling_transition_planned",
        "vertical_scaling_transition_applied_pending_validation",
        "vertical_scaling_reported",
    }:
        raise AwsRehostError("the approved Terraform plan has not been applied")

    status = invoke(
        runner,
        ("git", "status", "--porcelain"),
        action="Git worktree inspection",
    )
    if status.stdout.strip():
        raise AwsRehostError("Git worktree must be clean")
    revision = invoke(
        runner,
        ("git", "rev-parse", "HEAD"),
        action="Git revision inspection",
    ).stdout.strip()
    if not revision or manifest.get("git_revision") != revision:
        raise AwsRehostError("Git revision differs from the approved plan")
    return manifest, revision


def terraform_output(
    session: AwsSession,
    name: str,
    *,
    runner: ProcessRunner,
) -> str:
    """Read one sensitive infrastructure identifier without printing it."""
    result = invoke(
        runner,
        session.terraform_command("output", "-raw", name),
        action=f"Terraform output {name}",
    )
    value = result.stdout.strip()
    if not value:
        raise AwsRehostError(f"Terraform output {name} is empty")
    return value


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
        raise AwsRehostError("Terraform returned an invalid ECR repository URL")


def publish_image(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
) -> None:
    """Build and publish exactly one Linux AMD64 image, then record its digest."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    repository_url = terraform_output(
        session,
        "rehost_ecr_repository_url",
        runner=runner,
    )
    validate_repository_url(repository_url, region=session.region)
    registry_host, repository_name = repository_url.split("/", maxsplit=1)
    image_tag = f"git-{revision[:12]}"

    password_result = invoke(
        runner,
        (*aws_prefix(session), "ecr", "get-login-password"),
        action="ECR authentication",
    )
    if not password_result.stdout:
        raise AwsRehostError("ECR returned an empty login password")
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
        input_text=password_result.stdout,
        action="Docker ECR login",
    )
    try:
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
                "--tag",
                f"{repository_url}:{image_tag}",
                "--push",
                ".",
            ),
            action="AMD64 image publication",
        )
    finally:
        runner(("docker", "logout", registry_host), None)

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
        action="ECR image digest lookup",
    ).stdout.strip()
    if IMAGE_DIGEST_PATTERN.fullmatch(digest) is None:
        raise AwsRehostError("ECR returned an invalid image digest")

    manifest.update(
        {
            "image": {
                "architecture": "linux/amd64",
                "digest": digest,
                "tag": image_tag,
            },
            "image_published_at": datetime.now(UTC).isoformat(),
            "status": "image_published",
        }
    )
    write_manifest(session, manifest)


def encoded_file(path: Path) -> str:
    """Encode a committed deployment file for a secret-free SSM payload."""
    return b64encode(path.read_bytes()).decode("ascii")


def build_ssm_payload(
    *,
    files: RehostFiles,
    image_reference: str,
    region: str,
    smoke_suffix: str,
) -> dict[str, list[str]]:
    """Build one Run Command payload that contains no runtime secret."""
    files.validate()
    command = "\n".join(
        (
            "set -euo pipefail",
            "install -d -m 0700 /opt/trackrelay",
            (
                f"printf '%s' '{encoded_file(files.compose)}' | "
                "base64 --decode > /opt/trackrelay/compose.yaml"
            ),
            (
                f"printf '%s' '{encoded_file(files.installer)}' | "
                "base64 --decode > /opt/trackrelay/install.sh"
            ),
            "chmod 0600 /opt/trackrelay/compose.yaml",
            "chmod 0700 /opt/trackrelay/install.sh",
            f"TRACKRELAY_API_IMAGE='{image_reference}' \\",
            f"TRACKRELAY_AWS_REGION='{region}' \\",
            f"TRACKRELAY_POSTGRES_IMAGE='{POSTGRES_IMAGE}' \\",
            f"TRACKRELAY_SMOKE_SUFFIX='{smoke_suffix}' \\",
            "/opt/trackrelay/install.sh",
        )
    )
    if len(command.encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM deployment payload exceeds the local safety limit")
    return {"commands": [command], "executionTimeout": ["600"]}


def build_rds_ssm_payload(
    *,
    files: RehostFiles,
    image_reference: str,
    region: str,
    database_identifier: str,
    smoke_suffix: str,
) -> dict[str, list[str]]:
    """Build an RDS switch payload without its endpoint or credential."""
    files.validate()
    if RDS_IDENTIFIER_PATTERN.fullmatch(database_identifier) is None:
        raise AwsRehostError("invalid RDS database identifier")
    command = "\n".join(
        (
            "set -euo pipefail",
            "install -d -m 0700 /opt/trackrelay",
            (
                f"printf '%s' '{encoded_file(files.compose)}' | "
                "base64 --decode > /opt/trackrelay/compose.yaml"
            ),
            (
                f"printf '%s' '{encoded_file(files.installer)}' | "
                "base64 --decode > /opt/trackrelay/install-rds.sh"
            ),
            "chmod 0600 /opt/trackrelay/compose.yaml",
            "chmod 0700 /opt/trackrelay/install-rds.sh",
            f"TRACKRELAY_API_IMAGE='{image_reference}' \\",
            f"TRACKRELAY_AWS_REGION='{region}' \\",
            f"TRACKRELAY_RDS_IDENTIFIER='{database_identifier}' \\",
            f"TRACKRELAY_SMOKE_SUFFIX='{smoke_suffix}' \\",
            "/opt/trackrelay/install-rds.sh",
        )
    )
    if len(command.encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM RDS payload exceeds the local safety limit")
    return {"commands": [command], "executionTimeout": ["600"]}


def deployed_image_reference(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    revision: str,
    runner: ProcessRunner,
) -> str:
    """Resolve the approved digest-pinned image without persisting its URL."""
    image = manifest.get("image")
    if not isinstance(image, dict):
        raise AwsRehostError("no published image is recorded for this session")
    image_tag = image.get("tag")
    image_digest = image.get("digest")
    if not isinstance(image_tag, str) or not isinstance(image_digest, str):
        raise AwsRehostError("published image metadata is invalid")
    if image_tag != f"git-{revision[:12]}":
        raise AwsRehostError("published image tag differs from the approved revision")
    if IMAGE_DIGEST_PATTERN.fullmatch(image_digest) is None:
        raise AwsRehostError("published image digest is invalid")

    repository_url = terraform_output(
        session,
        "rehost_ecr_repository_url",
        runner=runner,
    )
    validate_repository_url(repository_url, region=session.region)
    return f"{repository_url}@{image_digest}"


def run_ssm_payload(
    session: AwsSession,
    *,
    instance_id: str,
    payload: dict[str, list[str]],
    comment: str,
    runner: ProcessRunner,
) -> str:
    """Send one secret-free payload, wait, and require successful status."""
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="trackrelay-ssm-",
        suffix=".json",
    ) as payload_file:
        payload_file.write(dumps(payload))
        payload_file.flush()
        command_id = invoke(
            runner,
            (
                *aws_prefix(session),
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--comment",
                comment,
                "--parameters",
                f"file://{payload_file.name}",
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ),
            action="SSM command submission",
        ).stdout.strip()
    if COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise AwsRehostError("SSM returned an invalid command ID")

    invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "wait",
            "command-executed",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
        ),
        action="SSM command completion",
    )
    invocation = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "[Status,ResponseCode]",
            "--output",
            "text",
        ),
        action="SSM command status",
    ).stdout.split()
    if invocation != ["Success", "0"]:
        raise AwsRehostError("SSM command did not report success")
    return command_id


def wait_for_ssm_online(
    session: AwsSession,
    instance_id: str,
    *,
    runner: ProcessRunner,
    sleeper: Sleeper,
    attempts: int = 30,
) -> None:
    """Wait a bounded time for the instance to report an online SSM agent."""
    for attempt in range(attempts):
        ping_status = invoke(
            runner,
            (
                *aws_prefix(session),
                "ssm",
                "describe-instance-information",
                "--filters",
                f"Key=InstanceIds,Values={instance_id}",
                "--query",
                "InstanceInformationList[0].PingStatus",
                "--output",
                "text",
            ),
            action="SSM instance readiness",
        ).stdout.strip()
        if ping_status == "Online":
            return
        if attempt < attempts - 1:
            sleeper(2)
    raise AwsRehostError("EC2 instance did not become online in SSM")


def deploy_rehost(
    session: AwsSession,
    *,
    files: RehostFiles,
    runner: ProcessRunner = run_process,
    sleeper: Sleeper = sleep,
    now: datetime | None = None,
) -> None:
    """Deploy the digest-pinned image through SSM and run a tiny smoke check."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    image_reference = deployed_image_reference(
        session,
        manifest=manifest,
        revision=revision,
        runner=runner,
    )
    instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")

    wait_for_ssm_online(
        session,
        instance_id,
        runner=runner,
        sleeper=sleeper,
    )

    deployed_at = now or datetime.now(UTC)
    smoke_suffix = f"{revision[:12]}-{deployed_at.strftime('%Y%m%dT%H%M%SZ')}"
    payload = build_ssm_payload(
        files=files,
        image_reference=image_reference,
        region=session.region,
        smoke_suffix=smoke_suffix,
    )
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=payload,
        comment="TrackRelay synchronous rehost deployment",
        runner=runner,
    )

    manifest.update(
        {
            "deployed_at": deployed_at.isoformat(),
            "deployment_command_id": command_id,
            "status": "rehost_deployed",
        }
    )
    write_manifest(session, manifest)


def deploy_rds_rehost(
    session: AwsSession,
    *,
    files: RehostFiles,
    canary_only: bool = False,
    runner: ProcessRunner = run_process,
    sleeper: Sleeper = sleep,
    now: datetime | None = None,
) -> None:
    """Switch the deployed rehost to private RDS and smoke-test persistence."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    required_status = (
        "rehost_deployed" if canary_only else "rehost_workload_collected"
    )
    if manifest.get("status") != required_status:
        raise AwsRehostError(
            "the required rehost checkpoint is incomplete before RDS"
        )
    image_reference = deployed_image_reference(
        session,
        manifest=manifest,
        revision=revision,
        runner=runner,
    )
    instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")
    resolved_engine_version = terraform_output(
        session,
        "rds_resolved_engine_version",
        runner=runner,
    )
    if RDS_ENGINE_VERSION_PATTERN.fullmatch(resolved_engine_version) is None:
        raise AwsRehostError("Terraform returned an invalid RDS engine version")

    wait_for_ssm_online(
        session,
        instance_id,
        runner=runner,
        sleeper=sleeper,
    )
    deployed_at = now or datetime.now(UTC)
    smoke_suffix = f"{revision[:12]}-{deployed_at.strftime('%Y%m%dT%H%M%SZ')}"
    resource_suffix = sha256(session.session_id.encode()).hexdigest()[:8]
    database_identifier = f"trackrelay-{resource_suffix}-postgres"
    payload = build_rds_ssm_payload(
        files=files,
        image_reference=image_reference,
        region=session.region,
        database_identifier=database_identifier,
        smoke_suffix=smoke_suffix,
    )
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=payload,
        comment="TrackRelay private RDS deployment",
        runner=runner,
    )
    manifest.update(
        {
            "rds": {
                "allocated_storage_gib": 20,
                "database_placement": "private-single-az-rds",
                "engine": "postgres",
                "engine_version": resolved_engine_version,
                "instance_class": "db.t4g.micro",
                "deployment_purpose": (
                    "sampler-canary" if canary_only else "stage-9.3"
                ),
                "storage_type": "encrypted-gp3",
            },
            "rds_deployed_at": deployed_at.isoformat(),
            "rds_deployment_command_id": command_id,
            "status": "rds_deployed",
        }
    )
    write_manifest(session, manifest)


def build_parser() -> ArgumentParser:
    """Build the local controller CLI without embedding cloud credentials."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_shared_arguments(subparsers.add_parser("publish"))
    deploy_parser = subparsers.add_parser("deploy")
    add_shared_arguments(deploy_parser)
    deploy_parser.add_argument("--compose-file", type=Path, required=True)
    deploy_parser.add_argument("--installer", type=Path, required=True)
    rds_parser = subparsers.add_parser("deploy-rds")
    add_shared_arguments(rds_parser)
    rds_parser.add_argument("--compose-file", type=Path, required=True)
    rds_parser.add_argument("--installer", type=Path, required=True)
    canary_rds_parser = subparsers.add_parser("deploy-rds-canary")
    add_shared_arguments(canary_rds_parser)
    canary_rds_parser.add_argument("--compose-file", type=Path, required=True)
    canary_rds_parser.add_argument("--installer", type=Path, required=True)
    add_shared_arguments(subparsers.add_parser("correctness-rds"))
    add_shared_arguments(subparsers.add_parser("workload"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Publish or deploy only after the guarded Terraform apply."""
    arguments: Namespace = build_parser().parse_args(argv)
    session = session_from_arguments(arguments)
    try:
        if arguments.command == "publish":
            publish_image(session)
            print("published the approved revision as a Linux AMD64 image")
        elif arguments.command == "deploy":
            deploy_rehost(
                session,
                files=RehostFiles(
                    compose=arguments.compose_file,
                    installer=arguments.installer,
                ),
            )
            print("deployed and smoke-tested the synchronous rehost through SSM")
        elif arguments.command in {"deploy-rds", "deploy-rds-canary"}:
            deploy_rds_rehost(
                session,
                files=RehostFiles(
                    compose=arguments.compose_file,
                    installer=arguments.installer,
                ),
                canary_only=arguments.command == "deploy-rds-canary",
            )
            print("switched the synchronous rehost to private RDS")
        elif arguments.command == "correctness-rds":
            from trackrelay.aws_rds_correctness import collect_rds_correctness

            evidence = collect_rds_correctness(session)
            print("collected all four RDS correctness scenarios")
            print(f"suite ID: {evidence.definition.suite_id}")
        else:
            from trackrelay.aws_rehost_workload import workload_from_arguments

            summary = workload_from_arguments(arguments)
            print("collected the frozen synchronous rehost workload evidence")
            print(
                "maximum sustainable rate: "
                f"{summary.maximum_sustainable_rate_per_second} events/s"
            )
    except (AwsRehostError, AwsSessionError) as error:
        raise SystemExit(f"AWS rehost command failed: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
