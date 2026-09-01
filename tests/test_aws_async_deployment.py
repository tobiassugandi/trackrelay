"""Tests for guarded, failure-safe asynchronous AWS deployment."""

from collections.abc import Sequence
from datetime import UTC, datetime
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess

from pytest import raises

import trackrelay.aws_async_deployment as async_deployment
from trackrelay.aws_async_deployment import (
    AwsAsyncDeploymentCleanupError,
    AwsAsyncDeploymentError,
    apply_async_phase,
    deploy_async_stack,
    publish_async_images,
    run_async_migration,
    stop_async_migration_tasks,
    validate_async_deployment_approval,
    wait_for_async_services,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)

SESSION_ID = "cloud-session-3-20260901T090000Z"
PROFILE = "trackrelay-admin"
REGION = "ap-southeast-3"
GIT_REVISION = "a" * 40
IMAGE_TAG = f"git-{GIT_REVISION[:12]}"
DIGESTS = {
    "api": f"sha256:{'a' * 64}",
    "worker": f"sha256:{'b' * 64}",
    "simulator": f"sha256:{'c' * 64}",
}
REPOSITORIES = {
    "rehost_ecr_repository_url": (
        "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-api"
    ),
    "worker_ecr_repository_url": (
        "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-worker"
    ),
    "simulator_ecr_repository_url": (
        "123456789012.dkr.ecr.ap-southeast-3.amazonaws.com/trackrelay-simulator"
    ),
}
CLUSTER_NAME = "trackrelay-8a7e37db-async"
SERVICE_NAMES = [
    "trackrelay-8a7e37db-api",
    "trackrelay-8a7e37db-simulator",
    "trackrelay-8a7e37db-worker",
]
TASK_DEFINITION_ARN = (
    "arn:aws:ecs:ap-southeast-3:123456789012:task-definition/"
    "trackrelay-8a7e37db-migration:1"
)
TASK_ARN = (
    "arn:aws:ecs:ap-southeast-3:123456789012:task/"
    "trackrelay-8a7e37db-async/0123456789abcdef0"
)


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, "")


def ready_session(
    tmp_path: Path,
    *,
    status: str = "applied",
) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        deployment_mode="async",
    )
    session.evidence_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "4.25",
            "deployment_mode": "async",
            "git_revision": GIT_REVISION,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": session.rehost_instance_type,
            "session_id": session.session_id,
            "status": status,
        },
    )
    return session


def approval_arguments(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "4.25",
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def terraform_output_name(arguments: Sequence[str]) -> str | None:
    call = tuple(arguments)
    if len(call) >= 5 and call[2] == "output":
        return call[4]
    return None


def git_result(arguments: Sequence[str]) -> CompletedProcess[str] | None:
    call = tuple(arguments)
    if call == ("git", "status", "--porcelain"):
        return completed(call)
    if call == ("git", "rev-parse", "HEAD"):
        return completed(call, stdout=GIT_REVISION)
    return None


def test_approval_requires_async_session_3_and_exact_cost(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)

    manifest = validate_async_deployment_approval(
        session,
        **approval_arguments(session),
    )

    assert manifest["status"] == "applied"
    with raises(AwsSessionError, match="cost ceiling"):
        validate_async_deployment_approval(
            session,
            **{
                **approval_arguments(session),
                "approved_cost_ceiling_usd": "4.26",
            },
        )


def test_publish_builds_all_targets_and_records_only_digests(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path, status="async_deployment_armed")
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append((call, input_text))
        git = git_result(call)
        if git is not None:
            return git
        output_name = terraform_output_name(call)
        if output_name is not None:
            return completed(call, stdout=REPOSITORIES[output_name])
        if "get-login-password" in call:
            return completed(call, stdout="temporary-ecr-password\n")
        if "describe-images" in call:
            repository_name = call[call.index("--repository-name") + 1]
            role = repository_name.rsplit("-", maxsplit=1)[-1]
            return completed(call, stdout=DIGESTS[role])
        return completed(call)

    published_at = datetime(2026, 9, 1, 10, tzinfo=UTC)
    observed = publish_async_images(
        session,
        runner=runner,
        now=published_at,
    )

    assert observed == DIGESTS
    build_calls = [call for call, _stdin in calls if "buildx" in call]
    assert len(build_calls) == 3
    for role, call in zip(("api", "worker", "simulator"), build_calls):
        assert call[call.index("--target") + 1] == role
        assert "linux/amd64" in call
        assert "--provenance=false" in call
        assert "--sbom=false" in call
        output_name = (
            "rehost_ecr_repository_url"
            if role == "api"
            else f"{role}_ecr_repository_url"
        )
        assert f"{REPOSITORIES[output_name]}:{IMAGE_TAG}" in call
    login_call, login_input = next(
        (call, stdin) for call, stdin in calls if "login" in call
    )
    assert "temporary-ecr-password" not in login_call
    assert login_input == "temporary-ecr-password\n"
    assert calls[-1][0][0:2] == ("docker", "logout")

    manifest_text = session.manifest_path.read_text(encoding="utf-8")
    manifest = loads(manifest_text)
    assert manifest["status"] == "async_images_published"
    assert manifest["async_images_published_at"] == published_at.isoformat()
    assert {
        role: manifest["async_images"][role]["digest"] for role in DIGESTS
    } == DIGESTS
    assert all(
        manifest["async_images"][role]["tag"] == IMAGE_TAG
        for role in DIGESTS
    )
    assert "123456789012" not in manifest_text
    assert "temporary-ecr-password" not in manifest_text


def test_publish_logs_out_when_one_target_fails(tmp_path: Path) -> None:
    session = ready_session(tmp_path, status="async_deployment_armed")
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        git = git_result(call)
        if git is not None:
            return git
        output_name = terraform_output_name(call)
        if output_name is not None:
            return completed(call, stdout=REPOSITORIES[output_name])
        if "get-login-password" in call:
            return completed(call, stdout="temporary-password")
        if "buildx" in call and call[call.index("--target") + 1] == "worker":
            return completed(call, returncode=1)
        if "describe-images" in call:
            return completed(call, stdout=DIGESTS["api"])
        return completed(call)

    with raises(AwsAsyncDeploymentError, match="worker.*publication"):
        publish_async_images(session, runner=runner)

    assert calls[-1][0:2] == ("docker", "logout")


def test_runtime_phase_applies_only_the_exact_saved_plan(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path, status="async_images_published")
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        git = git_result(call)
        if git is not None:
            return git
        if len(call) > 2 and call[2] == "plan":
            output_argument = next(item for item in call if item.startswith("-out="))
            Path(output_argument.removeprefix("-out=")).write_bytes(b"runtime-plan")
        return completed(call)

    applied_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    apply_async_phase(
        session,
        phase="runtime",
        image_digests=DIGESTS,
        services_enabled=False,
        runner=runner,
        now=applied_at,
    )

    terraform_calls = [call for call in calls if call[0] == "terraform"]
    assert len(terraform_calls) == 2
    plan_call, apply_call = terraform_calls
    assert "-var=deployment_mode=async" in plan_call
    assert "-var=async_services_enabled=false" in plan_call
    assert f"-var=api_image_digest={DIGESTS['api']}" in plan_call
    plan_path = session.evidence_dir / "terraform-async-runtime.tfplan"
    assert apply_call == session.terraform_command(
        "apply",
        "-input=false",
        plan_path.resolve().as_posix(),
    )
    manifest = load_manifest(session)
    assert manifest["status"] == "async_runtime_applied"
    assert len(manifest["async_phase_plans"]["runtime"]["plan_sha256"]) == 64
    assert not manifest["async_phase_plans"]["runtime"]["services_enabled"]


def test_runtime_phase_rejects_a_plan_changed_after_journaling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session = ready_session(tmp_path, status="async_images_published")
    calls: list[tuple[str, ...]] = []
    original_write_manifest = async_deployment.write_manifest

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        git = git_result(call)
        if git is not None:
            return git
        if len(call) > 2 and call[2] == "plan":
            output_argument = next(item for item in call if item.startswith("-out="))
            Path(output_argument.removeprefix("-out=")).write_bytes(b"exact-plan")
        return completed(call)

    def mutate_after_journal(
        current_session: AwsSession,
        manifest: dict[str, object],
    ) -> None:
        original_write_manifest(current_session, manifest)
        if manifest.get("status") == "async_runtime_planned":
            (current_session.evidence_dir / "terraform-async-runtime.tfplan").write_bytes(
                b"changed-plan"
            )

    monkeypatch.setattr(async_deployment, "write_manifest", mutate_after_journal)

    with raises(AwsAsyncDeploymentError, match="changed after planning"):
        apply_async_phase(
            session,
            phase="runtime",
            image_digests=DIGESTS,
            services_enabled=False,
            runner=runner,
        )

    assert not any(len(call) > 2 and call[2] == "apply" for call in calls)


def test_migration_uses_dedicated_network_and_records_no_arn(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path, status="async_runtime_applied")
    calls: list[tuple[str, ...]] = []
    migration_configuration = {
        "cluster_name": CLUSTER_NAME,
        "security_group_ids": ["sg-0123456789abcdef0"],
        "subnet_ids": ["subnet-0123456789abcdef0", "subnet-123456789abcdef01"],
        "task_definition_arn": TASK_DEFINITION_ARN,
    }

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        git = git_result(call)
        if git is not None:
            return git
        if terraform_output_name(call) == "async_migration_run_configuration":
            return completed(call, stdout=dumps(migration_configuration))
        if "run-task" in call:
            return completed(call, stdout=TASK_ARN)
        if "describe-tasks" in call:
            return completed(call, stdout='["STOPPED", "migration", 0]')
        return completed(call)

    completed_at = datetime(2026, 9, 1, 10, 10, tzinfo=UTC)
    run_async_migration(session, runner=runner, now=completed_at)

    run_call = next(call for call in calls if "run-task" in call)
    network = loads(run_call[run_call.index("--network-configuration") + 1])
    assert network == {
        "awsvpcConfiguration": {
            "assignPublicIp": "ENABLED",
            "securityGroups": migration_configuration["security_group_ids"],
            "subnets": migration_configuration["subnet_ids"],
        }
    }
    assert "FARGATE" in run_call
    assert "1.4.0" in run_call
    assert any("tasks-stopped" in call for call in calls)

    manifest_text = session.manifest_path.read_text(encoding="utf-8")
    manifest = loads(manifest_text)
    assert manifest["status"] == "async_migration_succeeded"
    assert manifest["async_migration"] == {
        "completed_at": completed_at.isoformat(),
        "exit_code": 0,
        "status": "succeeded",
    }
    assert "123456789012" not in manifest_text
    assert TASK_ARN not in manifest_text


def test_migration_rejects_nonzero_exit_before_services(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path, status="async_runtime_applied")
    migration_configuration = {
        "cluster_name": CLUSTER_NAME,
        "security_group_ids": ["sg-0123456789abcdef0"],
        "subnet_ids": ["subnet-0123456789abcdef0", "subnet-123456789abcdef01"],
        "task_definition_arn": TASK_DEFINITION_ARN,
    }

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        git = git_result(call)
        if git is not None:
            return git
        if terraform_output_name(call) == "async_migration_run_configuration":
            return completed(call, stdout=dumps(migration_configuration))
        if "run-task" in call:
            return completed(call, stdout=TASK_ARN)
        if "describe-tasks" in call:
            return completed(call, stdout='["STOPPED", "migration", 1]')
        return completed(call)

    with raises(AwsAsyncDeploymentError, match="did not exit successfully"):
        run_async_migration(session, runner=runner)

    assert load_manifest(session)["status"] == "async_migration_running"


def test_service_waiter_requires_exact_fixed_convergence(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path, status="async_services_applied")
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        git = git_result(call)
        if git is not None:
            return git
        output_name = terraform_output_name(call)
        if output_name == "async_ecs_cluster_name":
            return completed(call, stdout=CLUSTER_NAME)
        if output_name == "async_service_names":
            return completed(call, stdout=dumps(SERVICE_NAMES))
        if "describe-services" in call:
            return completed(
                call,
                stdout=dumps(
                    [
                        [name, "ACTIVE", 1, 1, 0, 1]
                        for name in reversed(SERVICE_NAMES)
                    ]
                ),
            )
        return completed(call)

    converged_at = datetime(2026, 9, 1, 10, 15, tzinfo=UTC)
    wait_for_async_services(session, runner=runner, now=converged_at)

    waiter_call = next(call for call in calls if "services-stable" in call)
    assert waiter_call[-3:] == tuple(SERVICE_NAMES)
    manifest = load_manifest(session)
    assert manifest["status"] == "async_deployed"
    assert manifest["async_fixed_services"] == {
        "api": 1,
        "simulator": 1,
        "worker": 1,
    }


def test_cleanup_stops_only_session_migration_tasks(tmp_path: Path) -> None:
    session = ready_session(tmp_path, status="async_migration_running")
    calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        _input_text: str | None,
    ) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        if "list-tasks" in call:
            desired_status = call[call.index("--desired-status") + 1]
            tasks = [TASK_ARN] if desired_status == "RUNNING" else []
            return completed(call, stdout=dumps(tasks))
        return completed(call, stdout=TASK_ARN)

    stop_async_migration_tasks(session, runner=runner)

    inventory_calls = [call for call in calls if "list-tasks" in call]
    assert len(inventory_calls) == 2
    assert all("--family" in call for call in inventory_calls)
    assert all(call[call.index("--family") + 1].endswith("-migration") for call in inventory_calls)
    stop_call = next(call for call in calls if "stop-task" in call)
    assert stop_call[stop_call.index("--task") + 1] == TASK_ARN
    waiter_call = next(call for call in calls if "tasks-stopped" in call)
    assert waiter_call[-1] == TASK_ARN


def test_complete_workflow_runs_in_order_and_leaves_successful_stack_on(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions: list[object] = []

    def apply_phase(_session, **kwargs):
        actions.append(("apply", kwargs["phase"], kwargs["services_enabled"]))

    def converge(current_session):
        actions.append("converge")
        manifest = load_manifest(current_session)
        manifest["status"] = "async_deployed"
        write_manifest(current_session, manifest)

    deploy_async_stack(
        session,
        **approval_arguments(session),
        publisher=lambda _session: actions.append("publish") or DIGESTS,
        phase_applier=apply_phase,
        migrator=lambda _session: actions.append("migrate"),
        convergence_waiter=converge,
        destroyer=lambda _session: actions.append("destroy"),
        teardown_verifier=lambda _session: actions.append("verify"),
    )

    assert actions == [
        "publish",
        ("apply", "runtime", False),
        "migrate",
        ("apply", "services", True),
        "converge",
    ]
    manifest = load_manifest(session)
    assert manifest["status"] == "async_deployed"
    assert manifest["async_deployment"]["unconditional_teardown_armed"]


def test_failure_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    def fail_migration(_session):
        actions.append("migrate")
        raise RuntimeError("simulated migration failure")

    with raises(RuntimeError, match="simulated migration failure"):
        deploy_async_stack(
            session,
            **approval_arguments(session),
            publisher=lambda _session: actions.append("publish") or DIGESTS,
            phase_applier=lambda _session, **kwargs: actions.append(
                f"apply-{kwargs['phase']}"
            ),
            migrator=fail_migration,
            convergence_waiter=lambda _session: actions.append("converge"),
            migration_task_stopper=lambda _session: actions.append("stop-migration"),
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == [
        "publish",
        "apply-runtime",
        "migrate",
        "stop-migration",
        "destroy",
        "verify",
    ]


def test_keyboard_interrupt_also_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    def interrupt(_session):
        raise KeyboardInterrupt("simulated interrupt")

    with raises(KeyboardInterrupt, match="simulated interrupt"):
        deploy_async_stack(
            session,
            **approval_arguments(session),
            publisher=lambda _session: DIGESTS,
            phase_applier=lambda *_args, **_kwargs: None,
            migrator=interrupt,
            convergence_waiter=lambda _session: None,
            migration_task_stopper=lambda _session: actions.append("stop-migration"),
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == ["stop-migration", "destroy", "verify"]


def test_cleanup_failure_preserves_the_workflow_failure(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    verification_calls: list[str] = []

    def fail_publication(_session):
        raise RuntimeError("simulated publication failure")

    def fail_destroy(_session):
        raise RuntimeError("simulated destroy failure")

    with raises(AwsAsyncDeploymentCleanupError) as failure:
        deploy_async_stack(
            session,
            **approval_arguments(session),
            publisher=fail_publication,
            migration_task_stopper=lambda _session: None,
            destroyer=fail_destroy,
            teardown_verifier=lambda _session: verification_calls.append("verify"),
        )

    assert str(failure.value.workflow_error) == "simulated publication failure"
    assert len(failure.value.cleanup_errors) == 1
    assert verification_calls == ["verify"]


def test_approval_mismatch_has_no_workflow_or_cleanup_side_effect(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    with raises(AwsSessionError, match="approved session ID"):
        deploy_async_stack(
            session,
            **{
                **approval_arguments(session),
                "approved_session_id": "cloud-session-3-20260901T100000Z",
            },
            publisher=lambda _session: actions.append("publish") or DIGESTS,
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == []
