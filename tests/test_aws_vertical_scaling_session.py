"""Tests for failure-safe Stage 9.3 orchestration and teardown."""

from dataclasses import replace
from pathlib import Path

from pytest import raises

from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)
from trackrelay.aws_vertical_scaling_session import (
    EXPECTED_TIER_ORDER,
    VerticalScalingSessionError,
    run_vertical_scaling_session,
)

SESSION_ID = "cloud-session-2-20260829T090000Z"


def ready_session(tmp_path: Path) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile="trackrelay-admin",
        region="ap-southeast-3",
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
    )
    session.evidence_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "3.50",
            "git_revision": "a" * 40,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": "t3.small",
            "session_id": session.session_id,
            "status": "rds_correctness_collected",
        },
    )
    return session


def approval_arguments(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "3.50",
        "approved_tier_order": EXPECTED_TIER_ORDER,
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def journal_transition(session: AwsSession, target_instance_type: str) -> None:
    manifest = load_manifest(session)
    target_session = replace(
        session,
        rehost_instance_type=target_instance_type,
    )
    manifest["rehost_instance_type"] = target_instance_type
    manifest["status"] = "vertical_scaling_ready"
    write_manifest(target_session, manifest)


def test_complete_session_runs_in_order_then_destroys_and_verifies(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions: list[tuple[str, str]] = []
    report = object()

    def preparer(current_session):
        actions.append(("prepare", current_session.rehost_instance_type))

    def tier_runner(current_session):
        actions.append(("tier", current_session.rehost_instance_type))

    def transition_runner(current_session, *, target_instance_type, **_kwargs):
        actions.append(
            (
                "transition",
                f"{current_session.rehost_instance_type}->{target_instance_type}",
            )
        )
        journal_transition(current_session, target_instance_type)

    def reporter(current_session):
        actions.append(("report", current_session.rehost_instance_type))
        return report

    def destroyer(current_session):
        actions.append(("destroy", current_session.rehost_instance_type))

    def verifier(current_session):
        actions.append(("verify", current_session.rehost_instance_type))

    observed = run_vertical_scaling_session(
        session,
        **approval_arguments(session),
        preparer=preparer,
        tier_runner=tier_runner,
        transition_runner=transition_runner,
        reporter=reporter,
        destroyer=destroyer,
        teardown_verifier=verifier,
    )

    assert observed is report
    assert actions == [
        ("prepare", "t3.small"),
        ("tier", "t3.small"),
        ("transition", "t3.small->c7i-flex.large"),
        ("tier", "c7i-flex.large"),
        ("report", "c7i-flex.large"),
        ("destroy", "c7i-flex.large"),
        ("verify", "c7i-flex.large"),
    ]


def test_tier_failure_still_destroys_and_verifies_the_journaled_tier(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions: list[tuple[str, str]] = []
    tier_count = 0

    def tier_runner(current_session):
        nonlocal tier_count
        tier_count += 1
        actions.append(("tier", current_session.rehost_instance_type))
        if tier_count == 2:
            raise RuntimeError("simulated c7i-flex.large failure")

    def transition_runner(current_session, *, target_instance_type, **_kwargs):
        journal_transition(current_session, target_instance_type)

    def destroyer(current_session):
        actions.append(("destroy", current_session.rehost_instance_type))

    def verifier(current_session):
        actions.append(("verify", current_session.rehost_instance_type))

    with raises(RuntimeError, match="simulated c7i-flex.large failure"):
        run_vertical_scaling_session(
            session,
            **approval_arguments(session),
            preparer=lambda _session: None,
            tier_runner=tier_runner,
            transition_runner=transition_runner,
            reporter=lambda _session: object(),
            destroyer=destroyer,
            teardown_verifier=verifier,
        )

    assert actions == [
        ("tier", "t3.small"),
        ("tier", "c7i-flex.large"),
        ("destroy", "c7i-flex.large"),
        ("verify", "c7i-flex.large"),
    ]


def test_transition_failure_cleans_up_the_pending_journaled_tier(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    cleanup_tiers = []

    def transition_runner(current_session, *, target_instance_type, **_kwargs):
        manifest = load_manifest(current_session)
        manifest["status"] = "vertical_scaling_transition_planned"
        manifest["vertical_scaling"] = {
            "pending_transition": {
                "source_instance_type": current_session.rehost_instance_type,
                "target_instance_type": target_instance_type,
            }
        }
        write_manifest(current_session, manifest)
        raise RuntimeError("simulated interrupted transition apply")

    with raises(RuntimeError, match="interrupted transition apply"):
        run_vertical_scaling_session(
            session,
            **approval_arguments(session),
            preparer=lambda _session: None,
            tier_runner=lambda _session: None,
            transition_runner=transition_runner,
            destroyer=lambda current_session: cleanup_tiers.append(
                ("destroy", current_session.rehost_instance_type)
            ),
            teardown_verifier=lambda current_session: cleanup_tiers.append(
                ("verify", current_session.rehost_instance_type)
            ),
        )

    assert cleanup_tiers == [
        ("destroy", "c7i-flex.large"),
        ("verify", "c7i-flex.large"),
    ]
    cleanup_manifest = load_manifest(
        replace(session, rehost_instance_type="c7i-flex.large")
    )
    assert cleanup_manifest["cleanup_selected_from"] == "pending_transition"
    assert cleanup_manifest["status"] == "vertical_scaling_cleanup_pending"


def test_destroy_failure_does_not_skip_native_verification(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    verification_calls = []

    def transition_runner(current_session, *, target_instance_type, **_kwargs):
        journal_transition(current_session, target_instance_type)

    def destroyer(_session):
        raise RuntimeError("simulated destroy failure")

    def verifier(current_session):
        verification_calls.append(current_session.rehost_instance_type)

    with raises(VerticalScalingSessionError, match="destroy failure") as failure:
        run_vertical_scaling_session(
            session,
            **approval_arguments(session),
            preparer=lambda _session: None,
            tier_runner=lambda _session: None,
            transition_runner=transition_runner,
            reporter=lambda _session: object(),
            destroyer=destroyer,
            teardown_verifier=verifier,
        )

    assert verification_calls == ["c7i-flex.large"]
    assert len(failure.value.cleanup_errors) == 1
    assert failure.value.workflow_error is None


def test_keyboard_interrupt_still_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    actions: list[tuple[str, str]] = []

    def tier_runner(current_session):
        actions.append(("tier", current_session.rehost_instance_type))
        raise KeyboardInterrupt("simulated operator interrupt")

    def destroyer(current_session):
        actions.append(("destroy", current_session.rehost_instance_type))

    def verifier(current_session):
        actions.append(("verify", current_session.rehost_instance_type))

    with raises(KeyboardInterrupt, match="simulated operator interrupt"):
        run_vertical_scaling_session(
            session,
            **approval_arguments(session),
            preparer=lambda _session: None,
            tier_runner=tier_runner,
            destroyer=destroyer,
            teardown_verifier=verifier,
        )

    assert actions == [
        ("tier", "t3.small"),
        ("destroy", "t3.small"),
        ("verify", "t3.small"),
    ]


def test_approval_mismatch_has_no_workflow_or_cleanup_side_effect(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions = []

    with raises(AwsSessionError, match="tier order"):
        run_vertical_scaling_session(
            session,
            **{
                **approval_arguments(session),
                "approved_tier_order": "t3.small,m7i-flex.large",
            },
            preparer=lambda _session: actions.append("prepare"),
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == []
