"""Run an elastic-only Stage 9.7 diagnostic without publishing a comparison."""

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from signal import SIGTERM, getsignal, signal
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, model_validator

from trackrelay.aws_async_deployment import (
    ProcessRunner,
    deploy_async_stack,
    run_process,
)
from trackrelay.aws_diagnostics import capture_ecs_diagnostics, error_evidence
from trackrelay.aws_elastic_treatment import (
    ELASTIC_TREATMENT_CONTRACT,
    AwsElasticTreatmentError,
    ElasticTreatmentContract,
    ElasticTreatmentQualification,
    evaluate_elastic_treatment,
    verify_elastic_environment,
)
from trackrelay.aws_elasticity_cloudwatch import (
    ElasticityCloudWatchEvidence,
    collect_elasticity_cloudwatch_evidence,
)
from trackrelay.aws_elasticity_session import (
    _CleanupOwner,
    validate_elasticity_session_approval,
)
from trackrelay.aws_elasticity_transition import (
    DiagnosticTransitionEvidence,
    execute_elasticity_transition,
)
from trackrelay.aws_fixed_control import (
    ElasticityResult,
    _prepare_fixed_control,
    execute_elasticity_workload,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    CommandRunner,
    add_shared_arguments,
    apply_session,
    destroy_session,
    load_manifest,
    run_command,
    session_from_arguments,
    verify_destroyed,
    write_manifest,
)
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    ElasticityWorkloadDefinition,
)
from trackrelay.operator_status import (
    operator_failure,
    operator_status,
    progress_output,
    status_activity,
)

SessionAction = Callable[..., object]


class AwsElasticityDiagnosticError(RuntimeError):
    """Preserve a diagnostic failure alongside independent cleanup failures."""

    def __init__(
        self,
        *,
        workflow_error: BaseException | None,
        cleanup_errors: Sequence[BaseException] = (),
    ) -> None:
        super().__init__(
            "elastic-only diagnostic did not complete; inspect retained evidence "
            "and verify teardown. This run is never comparison evidence. "
            f"Workflow: {error_evidence(workflow_error)}; "
            f"cleanup: {[error_evidence(error) for error in cleanup_errors]}"
        )
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


class ElasticityDiagnosticSummary(BaseModel):
    """Self-contained elastic evidence that cannot support a comparison claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["elastic-only-diagnostic"] = "elastic-only-diagnostic"
    headline_eligible: Literal[False] = False
    comparison_multiplier: None = None
    transition: DiagnosticTransitionEvidence
    contract: ElasticTreatmentContract
    measurement: ElasticityResult
    cloudwatch: ElasticityCloudWatchEvidence
    qualification: ElasticTreatmentQualification

    @model_validator(mode="after")
    def require_diagnostic_provenance(self) -> "ElasticityDiagnosticSummary":
        if (
            self.measurement.definition.name
            not in (
                "aws-elasticity-candidate-v3",
                "aws-elasticity-candidate-v4",
                "aws-elasticity-demo-v5",
                "aws-elasticity-demo-v6",
            )
            or self.transition.policy.policy_version not in (3, 4, 5)
            or self.measurement.load_started_at < self.transition.verified_at
        ):
            raise ValueError(
                "diagnostic requires a supported workload and policy timeline"
            )
        if self.transition.reset_fixed_test_run_id is not None:
            raise ValueError("elastic diagnostic must not claim a fixed-control reset")
        if self.qualification != evaluate_elastic_treatment(
            self.measurement,
            self.cloudwatch,
            policy=self.transition.policy,
            contract=self.contract,
        ):
            raise ValueError("elastic diagnostic decision differs from its evidence")
        return self


def validate_elasticity_diagnostic_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    monthly_budget_usd: str,
    runner: CommandRunner = run_command,
) -> None:
    """Require a fresh reviewed plan and explicit diagnostic spending approval."""
    validate_elasticity_session_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
        monthly_budget_usd=monthly_budget_usd,
        runner=runner,
    )


def _journal_diagnostic(
    session: AwsSession,
    phase: str,
    completed: Sequence[str],
    *,
    error: BaseException | None = None,
    cleanup_errors: Sequence[BaseException] = (),
    failed_phase: str | None = None,
    diagnostic_errors: Sequence[BaseException] = (),
) -> None:
    manifest = load_manifest(session)
    manifest["elasticity_diagnostic_session"] = {
        "kind": "elastic-only-diagnostic",
        "headline_eligible": False,
        "comparison_report_allowed": False,
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


def run_elastic_diagnostic_treatment(
    session: AwsSession,
    *,
    transition: DiagnosticTransitionEvidence,
    definition: ElasticityWorkloadDefinition = ELASTICITY_WORKLOAD_DEFINITION,
    treatment_runner: Callable[..., ElasticityResult] = execute_elasticity_workload,
    metric_collector: Callable[
        ..., ElasticityCloudWatchEvidence
    ] = collect_elasticity_cloudwatch_evidence,
    environment_verifier: Callable[..., None] = verify_elastic_environment,
    runner: ProcessRunner,
) -> ElasticityDiagnosticSummary:
    """Run the exact elastic treatment from a fresh, proven-empty deployment."""
    manifest = load_manifest(session)
    if manifest.get("status") != "worker_autoscaling_diagnostic_verified":
        raise AwsSessionError("elastic diagnostic requires verified worker autoscaling")
    if (
        definition != ELASTICITY_WORKLOAD_DEFINITION
        or transition.policy.policy_version not in (3, 4, 5)
    ):
        raise AwsSessionError(
            "elastic diagnostic requires the frozen workload and policy"
        )
    if transition.reset_fixed_test_run_id is not None:
        raise AwsElasticTreatmentError(
            "elastic diagnostic transition claims paired fixed-control evidence"
        )
    api_url, source_url, dlq_url, cluster, worker, root = _prepare_fixed_control(
        session,
        manifest=manifest,
        definition=definition,
        runner=runner,
        treatment=ElasticityTreatment.ELASTIC,
        evidence_name="diagnostic",
    )
    if transition.policy.resource_id != f"service/{cluster}/{worker}":
        raise AwsElasticTreatmentError("worker service identity changed")
    contract = ELASTIC_TREATMENT_CONTRACT
    (root / "contract.json").write_text(
        contract.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    environment_arguments = {
        "manifest": manifest,
        "policy": transition.policy,
        "api_url": api_url,
        "source_queue_url": source_url,
        "dead_letter_queue_url": dlq_url,
        "evidence_root": root,
        "runner": runner,
    }
    with status_activity(
        "Elastic diagnostic: verifying unchanged pre-load environment"
    ):
        environment_verifier(session, phase="before", **environment_arguments)
    result = treatment_runner(
        session,
        treatment=ElasticityTreatment.ELASTIC,
        api_url=api_url,
        source_queue_url=source_url,
        dead_letter_queue_url=dlq_url,
        cluster_name=cluster,
        worker_service_name=worker,
        evidence_root=root,
        definition=definition,
        runner=runner,
    )
    if (
        result.definition != definition
        or result.load_started_at < transition.verified_at
    ):
        raise AwsElasticTreatmentError("elastic diagnostic workload or timing changed")
    with status_activity("Elastic diagnostic: collecting CloudWatch evidence"):
        cloudwatch = metric_collector(
            session,
            test_run_id=result.test_run_id,
            window_started_at=result.load_started_at,
            window_ended_at=result.load_ended_at,
            runner=runner,
        )
    summary = ElasticityDiagnosticSummary(
        transition=transition,
        contract=contract,
        measurement=result,
        cloudwatch=cloudwatch,
        qualification=evaluate_elastic_treatment(
            result,
            cloudwatch,
            policy=transition.policy,
            contract=contract,
        ),
    )
    (root / "cloudwatch.json").write_text(
        cloudwatch.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (root / "summary.json").write_text(
        summary.model_dump_json(indent=2, round_trip=True) + "\n",
        encoding="utf-8",
    )
    with status_activity(
        "Elastic diagnostic: verifying unchanged post-load environment"
    ):
        environment_verifier(session, phase="after", **environment_arguments)
    manifest = load_manifest(session)
    manifest["elastic_diagnostic"] = {
        **manifest["elastic_diagnostic"],
        "headline_eligible": False,
        "comparison_multiplier": None,
        "test_run_id": str(result.test_run_id),
        "cloudwatch": "elasticity/diagnostic/elastic/cloudwatch.json",
        "result": "elasticity/diagnostic/elastic/result.json",
        "summary": "elasticity/diagnostic/elastic/summary.json",
        "qualified": summary.qualification.qualified,
    }
    manifest["status"] = (
        "elastic_diagnostic_qualified"
        if summary.qualification.qualified
        else "elastic_diagnostic_rejected"
    )
    write_manifest(session, manifest)
    if not summary.qualification.qualified:
        raise AwsElasticTreatmentError(
            "elastic diagnostic rejected: "
            + ", ".join(summary.qualification.rejection_reasons)
        )
    operator_status(
        "Elastic-only diagnostic qualified; no comparison multiplier will be produced"
    )
    return summary


def run_elasticity_diagnostic_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    monthly_budget_usd: str,
    runner: CommandRunner = run_command,
    process_runner: ProcessRunner = run_process,
    foundation_applier: SessionAction = apply_session,
    deployer: SessionAction = deploy_async_stack,
    transition_runner: SessionAction = execute_elasticity_transition,
    diagnostic_runner: SessionAction = run_elastic_diagnostic_treatment,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
    diagnostic_collector: SessionAction = capture_ecs_diagnostics,
) -> ElasticityDiagnosticSummary:
    """Provision, diagnose elasticity once, and always verify teardown."""
    approval = {
        "approved_session_id": approved_session_id,
        "approved_cost_ceiling_usd": approved_cost_ceiling_usd,
        "approved_unconditional_teardown_session_id": (
            approved_unconditional_teardown_session_id
        ),
    }
    validate_elasticity_diagnostic_approval(
        session,
        **approval,
        monthly_budget_usd=monthly_budget_usd,
        runner=runner,
    )
    operator_status(
        f"Elastic-only diagnostic {session.session_id} starting; approvals validated"
    )
    operator_status("Diagnostic evidence is not eligible for a comparison headline")
    operator_status(f"Evidence directory: {session.evidence_dir.resolve()}")
    operator_status(f"Live journal: {session.manifest_path.resolve()}")
    cleanup = _CleanupOwner(
        session, destroyer, teardown_verifier, diagnostic_collector, runner
    )
    completed: list[str] = []
    workflow_error: BaseException | None = None
    active_phase = "foundation"
    summary: ElasticityDiagnosticSummary | None = None
    try:
        _journal_diagnostic(session, "foundation", completed)
        with status_activity("Diagnostic phase 1/4: foundation apply"):
            foundation_applier(
                session,
                approved_session_id=approved_session_id,
                approved_cost_ceiling_usd=approved_cost_ceiling_usd,
                monthly_budget_usd=monthly_budget_usd,
                runner=runner,
            )
            if load_manifest(session).get("status") != "applied":
                raise AwsSessionError("foundation apply did not reach its checkpoint")
        completed.append("foundation")

        active_phase = "deployment"
        _journal_diagnostic(session, active_phase, completed)
        with status_activity("Diagnostic phase 2/4: async deployment"):
            deployer(
                session,
                **approval,
                destroyer=cleanup.destroy,
                teardown_verifier=cleanup.verify,
            )
            if load_manifest(session).get("status") != "async_deployed":
                raise AwsSessionError("deployment did not reach its checkpoint")
        completed.append(active_phase)

        active_phase = "transition"
        _journal_diagnostic(session, active_phase, completed)
        with status_activity("Diagnostic phase 3/4: worker-autoscaling transition"):
            transition = transition_runner(
                session,
                manifest=load_manifest(session),
                reset_result=None,
                runner=process_runner,
            )
            if not isinstance(transition, DiagnosticTransitionEvidence):
                raise AwsSessionError(
                    "autoscaling transition returned invalid evidence"
                )
            if transition.reset_fixed_test_run_id is not None:
                raise AwsSessionError("diagnostic transition claimed a fixed control")
            transition_root = session.evidence_dir / "elasticity/diagnostic/transition"
            saved_transition = DiagnosticTransitionEvidence.model_validate_json(
                (transition_root / "evidence.json").read_text(encoding="utf-8")
            )
            if saved_transition != transition:
                raise AwsSessionError(
                    "saved diagnostic transition differs from returned evidence"
                )
            manifest = load_manifest(session)
            recorded = manifest.get("worker_autoscaling_transition")
            manifest["worker_autoscaling_transition"] = {
                **(recorded if isinstance(recorded, dict) else {}),
                "diagnostic_only": True,
                "evidence": "elasticity/diagnostic/transition/evidence.json",
                "minimum_capacity": transition.policy.minimum_capacity,
                "maximum_capacity": transition.policy.maximum_capacity,
            }
            manifest["status"] = "worker_autoscaling_diagnostic_verified"
            write_manifest(session, manifest)
        completed.append(active_phase)

        active_phase = "elastic"
        _journal_diagnostic(session, active_phase, completed)
        with status_activity("Diagnostic phase 4/4: elastic treatment"):
            candidate = diagnostic_runner(
                session,
                transition=transition,
                runner=process_runner,
            )
            if not isinstance(candidate, ElasticityDiagnosticSummary):
                raise AwsSessionError("elastic diagnostic returned invalid evidence")
            if not candidate.qualification.qualified:
                raise AwsSessionError("elastic diagnostic was not qualified")
            if load_manifest(session).get("status") != "elastic_diagnostic_qualified":
                raise AwsSessionError("elastic diagnostic did not reach its checkpoint")
            summary = candidate
        completed.append(active_phase)
    except BaseException as error:  # noqa: BLE001 - cleanup must follow interrupts
        workflow_error = error
        operator_failure(f"Diagnostic phase {active_phase}", error)
    finally:
        cleanup.finish()
        operator_status(f"Evidence directory: {session.evidence_dir.resolve()}")

    try:
        _journal_diagnostic(
            session,
            "cloud_failed" if workflow_error or cleanup.errors else "cloud_complete",
            completed,
            error=workflow_error,
            cleanup_errors=cleanup.errors,
            failed_phase=active_phase if workflow_error else None,
            diagnostic_errors=cleanup.diagnostic_errors,
        )
    except BaseException as error:
        raise AwsElasticityDiagnosticError(
            workflow_error=workflow_error or error,
            cleanup_errors=cleanup.errors,
        ) from error
    if workflow_error is not None or cleanup.errors:
        raise AwsElasticityDiagnosticError(
            workflow_error=workflow_error,
            cleanup_errors=cleanup.errors,
        ) from workflow_error
    if summary is None:
        raise AwsElasticityDiagnosticError(
            workflow_error=AwsSessionError("diagnostic summary was not recorded")
        )
    return summary


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument("--approved-unconditional-teardown-session-id", required=True)
    parser.add_argument("--monthly-budget-usd", required=True)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress on stderr; retain conclusions and evidence.",
    )
    return parser


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during elastic-only diagnostic")


def main(argv: Sequence[str] | None = None) -> int:
    previous = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        arguments = build_parser().parse_args(argv)
        with progress_output(None) if arguments.quiet else progress_output():
            summary = run_elasticity_diagnostic_session(
                session_from_arguments(arguments),
                approved_session_id=arguments.approved_session_id,
                approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
                approved_unconditional_teardown_session_id=(
                    arguments.approved_unconditional_teardown_session_id
                ),
                monthly_budget_usd=arguments.monthly_budget_usd,
            )
        qualification = summary.qualification
        maximum_backlog = max(
            qualification.maximum_outstanding_events,
            qualification.maximum_native_queue_work,
            summary.measurement.maximum_source_queue_work,
        )
        print(
            "Elastic-only diagnostic qualified; no comparison multiplier was "
            "produced. Workers "
            f"{summary.transition.policy.minimum_capacity}→"
            f"{qualification.maximum_observed_workers}→"
            f"{summary.transition.policy.minimum_capacity}; "
            f"maximum backlog {maximum_backlog:g}. Teardown verified."
        )
    except (AwsSessionError, AwsElasticityDiagnosticError, KeyboardInterrupt) as error:
        raise SystemExit(f"AWS elasticity diagnostic failed: {error}") from error
    finally:
        signal(SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
