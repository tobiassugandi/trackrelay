"""Own an approved session-4 apply through both treatments and verified teardown."""

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from signal import SIGTERM, getsignal, signal
from typing import NoReturn

from trackrelay.aws_async_deployment import deploy_async_stack
from trackrelay.aws_diagnostics import capture_ecs_diagnostics, error_evidence
from trackrelay.aws_elastic_treatment import run_elastic_treatment_session
from trackrelay.aws_elasticity_report import (
    ElasticityComparisonReport,
    generate_elasticity_report,
)
from trackrelay.aws_elasticity_transition import run_elasticity_transition_session
from trackrelay.aws_experiment_reset import run_experiment_reset_session
from trackrelay.aws_fixed_control import run_fixed_control_session
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    CommandRunner,
    add_shared_arguments,
    apply_session,
    destroy_session,
    file_sha256,
    load_manifest,
    parse_positive_money,
    read_clean_git_revision,
    run_command,
    session_from_arguments,
    verify_destroyed,
    write_manifest,
)

SessionAction = Callable[..., object]


class ElasticitySessionError(RuntimeError):
    """Preserve the workflow failure alongside independent cleanup/report errors."""

    def __init__(
        self,
        *,
        workflow_error: BaseException | None,
        cleanup_errors: Sequence[BaseException] = (),
        report_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            "session 4 did not complete; inspect the retained phase journal and "
            "verify teardown before retrying any offline report. "
            f"Workflow: {error_evidence(workflow_error)}; "
            f"cleanup: {[error_evidence(error) for error in cleanup_errors]}; "
            f"report: {error_evidence(report_error)}"
        )
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)
        self.report_error = report_error


class _CleanupOwner:
    """Share one destroy/verify attempt across outer and nested controllers."""

    def __init__(
        self,
        session: AwsSession,
        destroyer: SessionAction,
        verifier: SessionAction,
        diagnostic_collector: SessionAction,
        runner: CommandRunner,
    ):
        self.session = session
        self.destroyer = destroyer
        self.verifier = verifier
        self.destroy_attempted = False
        self.verify_attempted = False
        self.errors: list[BaseException] = []
        self.diagnostic_errors: list[BaseException] = []
        self.diagnostic_collector = diagnostic_collector
        self.runner = runner

    def destroy(self, session: AwsSession) -> None:
        if self.destroy_attempted:
            return
        self.destroy_attempted = True
        try:
            if session != self.session:
                raise AwsSessionError("cleanup session identity changed")
            try:
                self.diagnostic_collector(session, runner=self.runner)
            except BaseException as error:  # noqa: BLE001 - even failed diagnostics must not block teardown
                self.diagnostic_errors.append(error)
            self.destroyer(session)
        except BaseException as error:
            self.errors.append(error)
            raise

    def verify(self, session: AwsSession) -> None:
        if self.verify_attempted:
            return
        self.verify_attempted = True
        try:
            if session != self.session:
                raise AwsSessionError("cleanup session identity changed")
            self.verifier(session)
            if load_manifest(session).get("status") != "teardown_verified":
                raise AwsSessionError("native teardown was not journaled as verified")
        except BaseException as error:
            self.errors.append(error)
            raise

    def finish(self) -> None:
        for action in (self.destroy, self.verify):
            try:
                action(self.session)
            except BaseException as error:  # noqa: BLE001 - retain any unexpected wrapper failure
                if error not in self.errors:
                    self.errors.append(error)


def validate_elasticity_session_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    monthly_budget_usd: str,
    runner: CommandRunner = run_command,
) -> None:
    """Reject mismatches before acquiring ownership or making cloud changes."""
    if (
        not session.session_id.startswith("cloud-session-4-")
        or session.deployment_mode != "async"
    ):
        raise AwsSessionError("elasticity session requires async cloud session 4")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    ceiling = parse_positive_money(
        approved_cost_ceiling_usd, field_name="approved cost ceiling"
    )
    budget = parse_positive_money(monthly_budget_usd, field_name="monthly budget")
    if ceiling > budget:
        raise AwsSessionError("approved cost ceiling exceeds the monthly budget")
    manifest = load_manifest(session)
    if manifest.get("status") != "planned" or "elasticity_session" in manifest:
        raise AwsSessionError(
            "session must start from a fresh reviewed plan, not a partial run"
        )
    if not session.plan_path.is_file() or file_sha256(
        session.plan_path
    ) != manifest.get("plan_sha256"):
        raise AwsSessionError("saved foundation plan is missing or changed")
    if read_clean_git_revision(runner) != manifest.get("git_revision"):
        raise AwsSessionError("Git revision differs from the approved plan")


def _journal(
    session: AwsSession,
    phase: str,
    completed: list[str],
    error: BaseException | None = None,
    cleanup_errors: Sequence[BaseException] = (),
    failed_phase: str | None = None,
    diagnostic_errors: Sequence[BaseException] = (),
) -> None:
    manifest = load_manifest(session)
    manifest["elasticity_session"] = {
        "phase": phase,
        "completed_phases": list(completed),
        "updated_at": datetime.now(UTC).isoformat(),
        "workflow_error_type": type(error).__name__ if error else None,
        "workflow_error": error_evidence(error),
        "cleanup_errors": [error_evidence(item) for item in cleanup_errors],
        "failed_phase": failed_phase,
        "diagnostic_errors": [error_evidence(item) for item in diagnostic_errors],
        "unconditional_teardown_armed": True,
    }
    write_manifest(session, manifest)


def run_elasticity_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    monthly_budget_usd: str,
    runner: CommandRunner = run_command,
    foundation_applier: SessionAction = apply_session,
    deployer: SessionAction = deploy_async_stack,
    fixed_runner: SessionAction = run_fixed_control_session,
    reset_runner: SessionAction = run_experiment_reset_session,
    transition_runner: SessionAction = run_elasticity_transition_session,
    elastic_runner: SessionAction = run_elastic_treatment_session,
    reporter: Callable[..., ElasticityComparisonReport] = generate_elasticity_report,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
    diagnostic_collector: SessionAction = capture_ecs_diagnostics,
) -> ElasticityComparisonReport:
    """Run once, clean up once, and render only after AWS absence is established."""
    approval = {
        "approved_session_id": approved_session_id,
        "approved_cost_ceiling_usd": approved_cost_ceiling_usd,
        "approved_unconditional_teardown_session_id": approved_unconditional_teardown_session_id,
    }
    validate_elasticity_session_approval(
        session, **approval, monthly_budget_usd=monthly_budget_usd, runner=runner
    )
    cleanup = _CleanupOwner(
        session, destroyer, teardown_verifier, diagnostic_collector, runner
    )
    completed: list[str] = []
    workflow_error: BaseException | None = None
    active_phase = "foundation"
    try:
        _journal(session, "foundation", completed)
        foundation_applier(
            session,
            approved_session_id=approved_session_id,
            approved_cost_ceiling_usd=approved_cost_ceiling_usd,
            monthly_budget_usd=monthly_budget_usd,
            runner=runner,
        )
        if load_manifest(session).get("status") != "applied":
            raise AwsSessionError(
                "foundation apply did not reach its required checkpoint"
            )
        completed.append("foundation")
        for name, action, expected_status in (
            ("deployment", deployer, "async_deployed"),
            ("fixed", fixed_runner, "fixed_control_qualified"),
            ("reset", reset_runner, "experiment_reset_verified"),
            ("transition", transition_runner, "worker_autoscaling_verified"),
            ("elastic", elastic_runner, "teardown_verified"),
        ):
            active_phase = name
            _journal(session, name, completed)
            action(
                session,
                **approval,
                destroyer=cleanup.destroy,
                teardown_verifier=cleanup.verify,
            )
            if load_manifest(session).get("status") != expected_status:
                raise AwsSessionError(f"{name} did not reach its required checkpoint")
            completed.append(name)
    except BaseException as error:  # noqa: BLE001 - include interrupts and journal failures
        workflow_error = error
    finally:
        cleanup.finish()
    # Finalize the journal before reporting: the report hashes session.json.
    # Cleanup failures must not skip this write and hide the initiating failure.
    try:
        _journal(
            session,
            "cloud_failed" if workflow_error or cleanup.errors else "cloud_complete",
            completed,
            workflow_error,
            cleanup_errors=cleanup.errors,
            failed_phase=active_phase if workflow_error else None,
            diagnostic_errors=cleanup.diagnostic_errors,
        )
    except BaseException as error:
        raise ElasticitySessionError(
            workflow_error=workflow_error or error, cleanup_errors=cleanup.errors
        ) from error
    if cleanup.errors:
        raise ElasticitySessionError(
            workflow_error=workflow_error, cleanup_errors=cleanup.errors
        ) from workflow_error
    report = None
    report_error = None
    elastic_evidence = load_manifest(session).get("elastic_treatment")
    if (
        isinstance(elastic_evidence, dict)
        and elastic_evidence.get("summary") == "elasticity/elastic/summary.json"
    ):
        try:
            report = reporter(session.evidence_dir)
        except BaseException as error:  # noqa: BLE001 - AWS is already verified absent
            report_error = error
    if workflow_error is not None or report_error is not None:
        raise ElasticitySessionError(
            workflow_error=workflow_error, report_error=report_error
        ) from (workflow_error or report_error)
    if report is None:
        raise ElasticitySessionError(
            workflow_error=AwsSessionError("elastic summary was not recorded")
        )
    return report


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument("--approved-unconditional-teardown-session-id", required=True)
    parser.add_argument("--monthly-budget-usd", required=True)
    return parser


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during cloud session 4")


def main(argv: Sequence[str] | None = None) -> int:
    previous = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        arguments = build_parser().parse_args(argv)
        report = run_elasticity_session(
            session_from_arguments(arguments),
            approved_session_id=arguments.approved_session_id,
            approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
            approved_unconditional_teardown_session_id=arguments.approved_unconditional_teardown_session_id,
            monthly_budget_usd=arguments.monthly_budget_usd,
        )
        print(report.conclusion)
    except (AwsSessionError, ElasticitySessionError, KeyboardInterrupt) as error:
        raise SystemExit(f"AWS elasticity session failed: {error}") from error
    finally:
        signal(SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
