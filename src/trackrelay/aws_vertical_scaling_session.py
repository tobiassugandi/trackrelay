"""Run Stage 9.3 treatments and always attempt verified teardown."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from signal import SIGTERM, getsignal, signal
from typing import NoReturn

from trackrelay.aws_flexibility_report import (
    HardwareFlexibilityReport,
    generate_hardware_flexibility_report,
)
from trackrelay.aws_rehost import AwsRehostError
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    destroy_session,
    load_manifest,
    parse_positive_money,
    session_from_arguments,
    verify_destroyed,
    write_manifest,
)
from trackrelay.aws_vertical_scaling import (
    prepare_vertical_scaling_experiment,
    run_current_vertical_scaling_tier,
    transition_to_next_vertical_scaling_tier,
)
from trackrelay.experiments.vertical_scaling import (
    ALLOWED_INSTANCE_TYPES,
    PRIMARY_FLEXIBILITY_INSTANCE_TYPES,
)

EXPECTED_TIER_ORDER = ",".join(PRIMARY_FLEXIBILITY_INSTANCE_TYPES)
SessionAction = Callable[..., object]
ReportAction = Callable[..., HardwareFlexibilityReport]


class VerticalScalingSessionError(RuntimeError):
    """Report a cleanup failure without hiding the benchmark failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException | None = None,
        cleanup_errors: Sequence[BaseException] = (),
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


def _approved_cost_ceiling(manifest: dict[str, object]) -> Decimal:
    value = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(value, str):
        raise AwsSessionError(
            "the applied session has no approved cost ceiling"
        )
    return parse_positive_money(value, field_name="recorded cost ceiling")


def validate_session_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_tier_order: str,
    approved_unconditional_teardown_session_id: str,
) -> None:
    """Arm the long-running workflow only after every approval matches."""
    if not session.session_id.startswith("cloud-session-2-"):
        raise AwsSessionError("Stage 9.3 must use cloud session 2")
    if session.rehost_instance_type != ALLOWED_INSTANCE_TYPES[0]:
        raise AwsSessionError("Stage 9.3 must start on the frozen first tier")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    if approved_tier_order != EXPECTED_TIER_ORDER:
        raise AwsSessionError("approved tier order does not match the frozen ladder")
    approved_ceiling = parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    )
    manifest = load_manifest(session)
    if manifest.get("status") != "rds_correctness_collected":
        raise AwsSessionError(
            "Stage 9.3 session automation requires completed RDS correctness"
        )
    if approved_ceiling != _approved_cost_ceiling(manifest):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")


def _session_matching_manifest(
    initial_session: AwsSession,
    fallback_session: AwsSession,
) -> AwsSession:
    """Find the current or pending journaled tier without trusting a guess."""
    for instance_type in reversed(ALLOWED_INSTANCE_TYPES):
        candidate = replace(
            initial_session,
            rehost_instance_type=instance_type,
        )
        try:
            manifest = load_manifest(candidate)
        except AwsSessionError:
            continue
        scaling = manifest.get("vertical_scaling")
        if isinstance(scaling, dict):
            pending_transition = scaling.get("pending_transition")
            if isinstance(pending_transition, dict):
                pending_target = pending_transition.get("target_instance_type")
                if pending_target in ALLOWED_INSTANCE_TYPES:
                    cleanup_session = replace(
                        initial_session,
                        rehost_instance_type=pending_target,
                    )
                    manifest["cleanup_selected_from"] = "pending_transition"
                    manifest["rehost_instance_type"] = pending_target
                    manifest["status"] = "vertical_scaling_cleanup_pending"
                    write_manifest(cleanup_session, manifest)
                    return cleanup_session
        return candidate
    return fallback_session


def run_vertical_scaling_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_tier_order: str,
    approved_unconditional_teardown_session_id: str,
    preparer: SessionAction = prepare_vertical_scaling_experiment,
    tier_runner: SessionAction = run_current_vertical_scaling_tier,
    transition_runner: SessionAction = transition_to_next_vertical_scaling_tier,
    reporter: ReportAction = generate_hardware_flexibility_report,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
) -> HardwareFlexibilityReport:
    """Run all treatments; after preflight, cleanup is never conditional."""
    validate_session_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_tier_order=approved_tier_order,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )

    current_session = session
    report: HardwareFlexibilityReport | None = None
    workflow_error: BaseException | None = None
    try:
        preparer(current_session)
        tier_runner(current_session)
        for target_instance_type in PRIMARY_FLEXIBILITY_INSTANCE_TYPES[1:]:
            transition_runner(
                current_session,
                target_instance_type=target_instance_type,
                approved_session_id=approved_session_id,
                approved_target_instance_type=target_instance_type,
            )
            current_session = replace(
                current_session,
                rehost_instance_type=target_instance_type,
            )
            tier_runner(current_session)
        report = reporter(current_session)
    except BaseException as error:  # noqa: BLE001 - teardown must follow interrupts
        workflow_error = error

    cleanup_session = _session_matching_manifest(session, current_session)
    cleanup_errors: list[BaseException] = []
    try:
        destroyer(cleanup_session)
    except BaseException as error:  # noqa: BLE001 - still run native verification
        cleanup_errors.append(error)
    try:
        teardown_verifier(cleanup_session)
    except BaseException as error:  # noqa: BLE001 - report every cleanup failure
        cleanup_errors.append(error)

    if cleanup_errors:
        message = (
            "Stage 9.3 cleanup did not complete: "
            + "; ".join(
                f"{type(error).__name__}: {error}"
                for error in cleanup_errors
            )
        )
        if workflow_error is not None:
            message = (
                f"Stage 9.3 workflow failed with "
                f"{type(workflow_error).__name__}: {workflow_error}; "
                + message
            )
        raise VerticalScalingSessionError(
            message,
            workflow_error=workflow_error,
            cleanup_errors=cleanup_errors,
        ) from (workflow_error or cleanup_errors[0])
    if workflow_error is not None:
        raise workflow_error
    if report is None:
        raise AssertionError("Stage 9.3 completed without a report")
    return report


def build_parser() -> ArgumentParser:
    """Build the explicitly armed cloud-session command."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument("--approved-tier-order", required=True)
    parser.add_argument(
        "--approved-unconditional-teardown-session-id",
        required=True,
    )
    return parser


def run_from_arguments(arguments: Namespace) -> HardwareFlexibilityReport:
    """Build the shared session object and execute the armed workflow."""
    return run_vertical_scaling_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_tier_order=arguments.approved_tier_order,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    """Translate SIGTERM into an exception so the cleanup path executes."""
    raise KeyboardInterrupt("received SIGTERM during Stage 9.3")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the armed workflow and translate ordinary failures concisely."""
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        report = run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsRehostError,
        AwsSessionError,
        KeyboardInterrupt,
        VerticalScalingSessionError,
    ) as error:
        raise SystemExit(f"AWS vertical-scaling session failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    print("completed Stage 9.3 and verified unconditional teardown")
    print(f"report generated at: {report.generated_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
