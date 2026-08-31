"""Tests for guarded AWS cloud-session lifecycle commands."""

from collections.abc import Sequence
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess

from pytest import mark, raises

from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    apply_session,
    destroy_session,
    load_manifest,
    plan_session,
    verify_destroyed,
)
from trackrelay.experiments.vertical_scaling import (
    COMPUTE_OPTIMIZED_INSTANCE_TYPE,
    MEMORY_OPTIMIZED_INSTANCE_TYPE,
)

SESSION_ID = "cloud-session-1-20260822T090000Z"
PROFILE = "trackrelay-admin"
REGION = "ap-southeast-3"
API_INGRESS_CIDR = "203.0.113.10/32"
GIT_REVISION = "a" * 40


def empty_native_inventory(**_: object) -> dict[str, int]:
    return {}


def make_session(tmp_path: Path) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    return AwsSession(
        session_id=SESSION_ID,
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr=API_INGRESS_CIDR,
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
    )


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, "")


def create_saved_plan(session: AwsSession) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        output_argument = next(
            argument for argument in call if argument.startswith("-out=")
        )
        Path(output_argument.removeprefix("-out=")).write_bytes(b"exact-plan")
        return completed(call, stdout="No changes.\n")

    plan_session(
        session,
        runner=runner,
        git_revision=GIT_REVISION,
    )
    return calls


def test_session_rejects_an_unstructured_identifier(tmp_path: Path) -> None:
    with raises(AwsSessionError, match="session ID"):
        AwsSession(
            session_id="session-one",
            profile=PROFILE,
            region=REGION,
            api_ingress_cidr=API_INGRESS_CIDR,
            terraform_dir=tmp_path,
            evidence_root=tmp_path,
        )


def test_session_accepts_the_fourth_planned_cloud_session(
    tmp_path: Path,
) -> None:
    session = AwsSession(
        session_id="cloud-session-4-20260822T090000Z",
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr=API_INGRESS_CIDR,
        terraform_dir=tmp_path,
        evidence_root=tmp_path,
    )

    assert session.session_id == "cloud-session-4-20260822T090000Z"


def test_plan_saves_exact_inputs_and_plan_identity(tmp_path: Path) -> None:
    session = make_session(tmp_path)

    calls = create_saved_plan(session)

    assert calls == [
        session.terraform_command(
            "plan",
            "-input=false",
            f"-out={session.plan_path.resolve()}",
            *session.terraform_variables(),
        )
    ]
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_id"] == SESSION_ID
    assert manifest["profile"] == PROFILE
    assert manifest["region"] == REGION
    assert manifest["api_ingress_cidr"] == API_INGRESS_CIDR
    assert manifest["deployment_mode"] == "rehost"
    assert manifest["rehost_instance_type"] == "t3.small"
    assert manifest["git_revision"] == GIT_REVISION
    assert manifest["status"] == "planned"
    assert len(manifest["plan_sha256"]) == 64


def test_async_plan_freezes_the_exclusive_deployment_mode(
    tmp_path: Path,
) -> None:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr=API_INGRESS_CIDR,
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        deployment_mode="async",
    )

    calls = create_saved_plan(session)

    assert "-var=deployment_mode=async" in calls[0]
    assert load_manifest(session)["deployment_mode"] == "async"


@mark.parametrize(
    "instance_type",
    (COMPUTE_OPTIMIZED_INSTANCE_TYPE, MEMORY_OPTIMIZED_INSTANCE_TYPE),
)
def test_plan_records_each_explicit_nondefault_hardware_tier(
    tmp_path: Path,
    instance_type: str,
) -> None:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile=PROFILE,
        region=REGION,
        api_ingress_cidr=API_INGRESS_CIDR,
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        rehost_instance_type=instance_type,
    )

    calls = create_saved_plan(session)

    assert (
        f"-var=rehost_instance_type={instance_type}"
        in calls[0]
    )
    manifest = load_manifest(session)
    assert manifest["rehost_instance_type"] == instance_type


def test_session_evidence_rejects_a_different_hardware_tier(
    tmp_path: Path,
) -> None:
    control_session = make_session(tmp_path)
    create_saved_plan(control_session)
    treatment_session = AwsSession(
        session_id=control_session.session_id,
        profile=control_session.profile,
        region=control_session.region,
        api_ingress_cidr=control_session.api_ingress_cidr,
        terraform_dir=control_session.terraform_dir,
        evidence_root=control_session.evidence_root,
        rehost_instance_type=MEMORY_OPTIMIZED_INSTANCE_TYPE,
    )

    with raises(AwsSessionError, match="rehost_instance_type"):
        load_manifest(treatment_session)


def test_session_evidence_rejects_a_different_deployment_mode(
    tmp_path: Path,
) -> None:
    control_session = make_session(tmp_path)
    create_saved_plan(control_session)
    async_session = AwsSession(
        session_id=control_session.session_id,
        profile=control_session.profile,
        region=control_session.region,
        api_ingress_cidr=control_session.api_ingress_cidr,
        terraform_dir=control_session.terraform_dir,
        evidence_root=control_session.evidence_root,
        deployment_mode="async",
    )

    with raises(AwsSessionError, match="deployment_mode"):
        load_manifest(async_session)


def test_legacy_session_evidence_defaults_to_rehost_mode(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    create_saved_plan(session)
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    del manifest["deployment_mode"]
    session.manifest_path.write_text(
        dumps(manifest),
        encoding="utf-8",
    )

    assert load_manifest(session)["session_id"] == SESSION_ID


def test_plan_refuses_to_replace_existing_session_evidence(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    create_saved_plan(session)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments)

    with raises(AwsSessionError, match="choose a new ID"):
        plan_session(
            session,
            runner=runner,
            git_revision=GIT_REVISION,
        )

    assert calls == []


def test_apply_requires_the_exact_approved_session_id(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments)

    with raises(AwsSessionError, match="exactly match"):
        apply_session(
            session,
            approved_session_id="cloud-session-2-20260822T090000Z",
            approved_cost_ceiling_usd="5",
            monthly_budget_usd="25",
            runner=runner,
            git_revision=GIT_REVISION,
        )

    assert calls == []


def test_apply_rejects_a_ceiling_above_the_monthly_budget(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)

    with raises(AwsSessionError, match="exceeds"):
        apply_session(
            session,
            approved_session_id=SESSION_ID,
            approved_cost_ceiling_usd="25.01",
            monthly_budget_usd="25",
            git_revision=GIT_REVISION,
        )


def test_apply_uses_only_the_unchanged_saved_plan(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    create_saved_plan(session)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments, stdout="Apply complete!\n")

    apply_session(
        session,
        approved_session_id=SESSION_ID,
        approved_cost_ceiling_usd="5.00",
        monthly_budget_usd="25",
        runner=runner,
        git_revision=GIT_REVISION,
    )

    assert calls == [
        session.terraform_command(
            "apply",
            "-input=false",
            session.plan_path.resolve().as_posix(),
        )
    ]
    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert manifest["approved_cost_ceiling_usd"] == "5.00"
    assert manifest["status"] == "applied"


def test_destroy_has_no_approval_gate(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments)

    destroy_session(session, runner=runner)

    assert len(calls) == 2
    assert calls[0][2:5] == ("plan", "-destroy", "-input=false")
    assert calls[1][2:4] == ("apply", "-input=false")


def test_destroy_rejects_a_mode_mismatch_before_terraform(
    tmp_path: Path,
) -> None:
    control_session = make_session(tmp_path)
    create_saved_plan(control_session)
    async_session = AwsSession(
        session_id=control_session.session_id,
        profile=control_session.profile,
        region=control_session.region,
        api_ingress_cidr=control_session.api_ingress_cidr,
        terraform_dir=control_session.terraform_dir,
        evidence_root=control_session.evidence_root,
        deployment_mode="async",
    )
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments)

    with raises(AwsSessionError, match="deployment_mode"):
        destroy_session(async_session, runner=runner)

    assert calls == []


def test_verification_rejects_a_mode_mismatch_before_inventory(
    tmp_path: Path,
) -> None:
    control_session = make_session(tmp_path)
    create_saved_plan(control_session)
    async_session = AwsSession(
        session_id=control_session.session_id,
        profile=control_session.profile,
        region=control_session.region,
        api_ingress_cidr=control_session.api_ingress_cidr,
        terraform_dir=control_session.terraform_dir,
        evidence_root=control_session.evidence_root,
        deployment_mode="async",
    )
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        calls.append(tuple(arguments))
        return completed(arguments)

    with raises(AwsSessionError, match="deployment_mode"):
        verify_destroyed(
            async_session,
            runner=runner,
            native_inventory=empty_native_inventory,
        )

    assert calls == []


def test_generic_verification_accepts_empty_state_and_inventory(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        calls.append(call)
        if call[0] == "terraform":
            return completed(call)
        return completed(call, stdout='{"ResourceTagMappingList": []}')

    verify_destroyed(
        session,
        runner=runner,
        native_inventory=empty_native_inventory,
    )

    inventory = loads(
        (session.evidence_dir / "aws-inventory-after-destroy.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory == {
        "get_resources_semantics": "tagged-or-previously-tagged",
        "returned_record_count": 0,
        "session_id": SESSION_ID,
    }
    assert calls[0] == session.terraform_command("state", "list")
    assert calls[1][0:6] == (
        "aws",
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "resourcegroupstaggingapi",
    )


def test_generic_verification_accepts_that_state_never_existed(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if call[0] == "terraform":
            return CompletedProcess(
                call,
                1,
                "",
                "No state file was found!",
            )
        return completed(call, stdout='{"ResourceTagMappingList": []}')

    verify_destroyed(
        session,
        runner=runner,
        native_inventory=empty_native_inventory,
    )

    inventory = loads(
        (session.evidence_dir / "aws-inventory-after-destroy.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["returned_record_count"] == 0


def test_generic_verification_records_tag_tombstones_without_identifiers(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if call[0] == "terraform":
            return completed(call)
        return completed(
            call,
            stdout=(
                '{"ResourceTagMappingList": '
                '[{"ResourceARN": "not-persisted"}]}'
            ),
        )

    verify_destroyed(
        session,
        runner=runner,
        native_inventory=empty_native_inventory,
    )

    inventory = loads(
        (session.evidence_dir / "aws-inventory-after-destroy.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["returned_record_count"] == 1
    assert inventory["get_resources_semantics"] == (
        "tagged-or-previously-tagged"
    )
    assert "ResourceARN" not in inventory


def test_verification_rejects_resources_found_by_native_checks(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)

    def runner(arguments: Sequence[str]) -> CompletedProcess[str]:
        call = tuple(arguments)
        if call[0] == "terraform":
            return completed(call)
        return completed(call, stdout='{"ResourceTagMappingList": []}')

    def native_inventory(**_: object) -> dict[str, int]:
        return {"ec2_instances": 1, "ecr_repositories": 0}

    with raises(AwsSessionError, match="ec2_instances"):
        verify_destroyed(
            session,
            runner=runner,
            native_inventory=native_inventory,
        )
