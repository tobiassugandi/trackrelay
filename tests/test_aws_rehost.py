"""Tests for digest-pinned ARM publication and secret-free SSM deployment."""

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess

from pytest import raises

from trackrelay.aws_rehost import (
    AwsRehostError,
    RehostFiles,
    deploy_rds_rehost,
    deploy_rehost,
    publish_image,
    wait_for_ssm_online,
)
from trackrelay.aws_session import AwsSession, write_manifest

SESSION_ID = "cloud-session-1-20260826T090000Z"
PROFILE = "trackrelay-admin"
REGION = "ap-southeast-3"
API_INGRESS_CIDR = "203.0.113.10/32"
GIT_REVISION = "a" * 40
REPOSITORY_URL = (
    "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-test-api"
)
INSTANCE_ID = "i-0123456789abcdef0"
IMAGE_DIGEST = "sha256:" + "b" * 64
COMMAND_ID = "11111111-2222-3333-4444-555555555555"
RDS_INSTALLER = (
    Path(__file__).resolve().parents[1] / "deploy" / "rehost" / "install-rds.sh"
)
REHOST_COMPOSE = (
    Path(__file__).resolve().parents[1] / "deploy" / "rehost" / "compose.yaml"
)


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, "")


def make_session(tmp_path: Path, *, status: str = "applied") -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr=API_INGRESS_CIDR,
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
    )
    session.evidence_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": API_INGRESS_CIDR,
            "git_revision": GIT_REVISION,
            "profile": PROFILE,
            "region": REGION,
            "session_id": SESSION_ID,
            "status": status,
        },
    )
    return session


def terraform_output_name(arguments: tuple[str, ...]) -> str | None:
    if arguments[0] != "terraform" or "output" not in arguments:
        return None
    return arguments[-1]


def test_downstream_capacity_is_fixed_independently_of_the_ec2_host() -> None:
    compose = REHOST_COMPOSE.read_text(encoding="utf-8")
    downstream_service = compose.split("\n  downstream:\n", 1)[1].split(
        "\n  api:\n",
        1,
    )[0]

    assert "\n    cpus: 1.0\n" in downstream_service
    assert "\n    mem_limit: 256m\n" in downstream_service


def test_rds_installer_waits_for_readiness_after_api_restart() -> None:
    installer = RDS_INSTALLER.read_text(encoding="utf-8")
    restart_position = installer.index("compose restart --timeout 10 api")
    readiness_position = installer.index(
        "wait_for_api_readiness",
        restart_position,
    )
    persisted_read_position = installer.index(
        'http://127.0.0.1:8000/api/v1/shipments/${tracking_number}',
        readiness_position,
    )

    assert restart_position < readiness_position < persisted_read_position


def test_publish_pushes_only_arm64_and_records_digest_without_credentials(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append((call, input_text))
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=f"{GIT_REVISION}\n")
        if terraform_output_name(call) == "rehost_ecr_repository_url":
            return completed(call, stdout=f"{REPOSITORY_URL}\n")
        if "get-login-password" in call:
            return completed(call, stdout="temporary-ecr-password\n")
        if "describe-images" in call:
            return completed(call, stdout=f"{IMAGE_DIGEST}\n")
        return completed(call)

    publish_image(session, runner=runner)

    build_call = next(call for call, _ in calls if "buildx" in call)
    assert "linux/arm64" in build_call
    assert "--push" in build_call
    assert f"{REPOSITORY_URL}:git-{GIT_REVISION[:12]}" in build_call
    login_call, login_input = next(
        (call, stdin) for call, stdin in calls if "login" in call
    )
    assert "temporary-ecr-password" not in login_call
    assert login_input == "temporary-ecr-password\n"
    assert any("logout" in call for call, _ in calls)

    manifest_text = session.manifest_path.read_text(encoding="utf-8")
    manifest = loads(manifest_text)
    assert manifest["status"] == "image_published"
    assert manifest["image"] == {
        "architecture": "linux/arm64",
        "digest": IMAGE_DIGEST,
        "tag": f"git-{GIT_REVISION[:12]}",
    }
    assert "temporary-ecr-password" not in manifest_text
    assert "123456789012" not in manifest_text


def test_publish_logs_out_when_the_arm_build_fails(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        if terraform_output_name(call) == "rehost_ecr_repository_url":
            return completed(call, stdout=REPOSITORY_URL)
        if "get-login-password" in call:
            return completed(call, stdout="temporary-password")
        if "buildx" in call:
            return completed(call, returncode=1)
        return completed(call)

    with raises(AwsRehostError, match="publication failed"):
        publish_image(session, runner=runner)

    assert "logout" in calls[-1]


def test_deploy_uses_ssm_without_ssh_or_plaintext_runtime_secrets(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="image_published")
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    manifest["image"] = {
        "architecture": "linux/arm64",
        "digest": IMAGE_DIGEST,
        "tag": f"git-{GIT_REVISION[:12]}",
    }
    write_manifest(session, manifest)
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    installer = tmp_path / "install.sh"
    installer.write_text(
        "#!/usr/bin/env bash\nopenssl rand -hex 24\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []
    captured_payload: dict[str, list[str]] = {}

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        if terraform_output_name(call) == "rehost_ecr_repository_url":
            return completed(call, stdout=REPOSITORY_URL)
        if terraform_output_name(call) == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if "describe-instance-information" in call:
            return completed(call, stdout="Online\n")
        if "send-command" in call:
            parameter = call[call.index("--parameters") + 1]
            payload_path = Path(parameter.removeprefix("file://"))
            captured_payload.update(loads(payload_path.read_text(encoding="utf-8")))
            return completed(call, stdout=COMMAND_ID)
        if "get-command-invocation" in call:
            return completed(call, stdout="Success\t0\n")
        return completed(call)

    deployed_at = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
    deploy_rehost(
        session,
        files=RehostFiles(compose=compose_file, installer=installer),
        runner=runner,
        sleeper=lambda _: None,
        now=deployed_at,
    )

    assert all(call[:5] == ("aws", "--profile", PROFILE, "--region", REGION)
               for call in calls if call[0] == "aws")
    assert not any("ssh" in argument.lower() for call in calls for argument in call)
    command = captured_payload["commands"][0]
    assert f"{REPOSITORY_URL}@{IMAGE_DIGEST}" in command
    assert "TRACKRELAY_SMOKE_SUFFIX='aaaaaaaaaaaa-20260826T103000Z'" in command
    assert "POSTGRES_PASSWORD=" not in command
    assert "replace-with-random" not in command
    assert captured_payload["executionTimeout"] == ["600"]

    saved_manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert saved_manifest["status"] == "rehost_deployed"
    assert saved_manifest["deployment_command_id"] == COMMAND_ID
    assert saved_manifest["deployed_at"] == deployed_at.isoformat()
    assert "123456789012" not in dumps(saved_manifest)


def test_deploy_refuses_an_image_from_a_different_revision(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="image_published")
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    manifest["image"] = {
        "architecture": "linux/arm64",
        "digest": IMAGE_DIGEST,
        "tag": "git-different0000",
    }
    write_manifest(session, manifest)
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        return completed(call)

    with raises(AwsRehostError, match="tag differs"):
        deploy_rehost(
            session,
            files=RehostFiles(tmp_path / "compose", tmp_path / "install"),
            runner=runner,
        )

    assert not any(call[0] == "aws" for call in calls)


def test_rds_deploy_discovers_connection_data_on_host_without_persisting_it(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="rehost_workload_collected")
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    manifest["image"] = {
        "architecture": "linux/arm64",
        "digest": IMAGE_DIGEST,
        "tag": f"git-{GIT_REVISION[:12]}",
    }
    write_manifest(session, manifest)
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    installer = tmp_path / "install-rds.sh"
    installer.write_text(
        "#!/usr/bin/env bash\naws secretsmanager get-secret-value\n",
        encoding="utf-8",
    )
    captured_payload: dict[str, list[str]] = {}

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        output_name = terraform_output_name(call)
        if output_name == "rehost_ecr_repository_url":
            return completed(call, stdout=REPOSITORY_URL)
        if output_name == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if output_name == "rds_resolved_engine_version":
            return completed(call, stdout="17.6")
        if "describe-instance-information" in call:
            return completed(call, stdout="Online")
        if "send-command" in call:
            parameter = call[call.index("--parameters") + 1]
            payload_path = Path(parameter.removeprefix("file://"))
            captured_payload.update(loads(payload_path.read_text(encoding="utf-8")))
            return completed(call, stdout=COMMAND_ID)
        if "get-command-invocation" in call:
            return completed(call, stdout="Success\t0")
        return completed(call)

    deployed_at = datetime(2026, 8, 27, 9, tzinfo=UTC)
    deploy_rds_rehost(
        session,
        files=RehostFiles(compose=compose_file, installer=installer),
        runner=runner,
        sleeper=lambda _: None,
        now=deployed_at,
    )

    suffix = sha256(SESSION_ID.encode()).hexdigest()[:8]
    command = captured_payload["commands"][0]
    assert (
        f"TRACKRELAY_RDS_IDENTIFIER='trackrelay-{suffix}-postgres'"
        in command
    )
    assert "rds_endpoint" not in command
    assert "secret_arn" not in command
    assert "POSTGRES_PASSWORD=" not in command

    saved_text = session.manifest_path.read_text(encoding="utf-8")
    saved_manifest = loads(saved_text)
    assert saved_manifest["status"] == "rds_deployed"
    assert saved_manifest["rds"] == {
        "allocated_storage_gib": 20,
        "database_placement": "private-single-az-rds",
        "engine": "postgres",
        "engine_version": "17.6",
        "instance_class": "db.t4g.micro",
        "storage_type": "encrypted-gp3",
    }
    assert "123456789012" not in saved_text
    assert "rds.amazonaws.com" not in saved_text


def test_ssm_readiness_poll_is_bounded_and_uses_ping_status(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    statuses = iter(("Offline", "Online"))
    sleeps: list[float] = []
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        return completed(call, stdout=next(statuses))

    wait_for_ssm_online(
        session,
        INSTANCE_ID,
        runner=runner,
        sleeper=sleeps.append,
        attempts=3,
    )

    assert sleeps == [2]
    assert len(calls) == 2
    assert all("describe-instance-information" in call for call in calls)
    assert all("InstanceInformationList[0].PingStatus" in call for call in calls)
