"""Replay and qualify Stage 9.7, then unconditionally tear down session 4."""

from argparse import Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from signal import SIGTERM, getsignal, signal
from typing import Literal, NoReturn
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_async_deployment import (
    AwsAsyncDeploymentError,
    ProcessRunner,
    require_clean_approved_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_elasticity_cloudwatch import (
    AwsElasticityCloudWatchError,
    ElasticityCloudWatchEvidence,
    collect_elasticity_cloudwatch_evidence,
)
from trackrelay.aws_elasticity_transition import (
    AwsElasticityTransitionError,
    ElasticityTransitionEvidence,
    WorkerAutoscalingPolicy,
    WorkerAutoscalingVerification,
    _image_digests,
    _terraform_variables,
    _transition_observation,
    verify_native_worker_autoscaling,
)
from trackrelay.aws_fixed_control import (
    AwsFixedControlError,
    ElasticityGuardrailQualification,
    ElasticityResult,
    FixedControlSummary,
    _prepare_fixed_control,
    _worker_counts,
    evaluate_fixed_control_qualification,
    evaluate_treatment_guardrails,
    execute_elasticity_workload,
)
from trackrelay.aws_fixed_control import (
    build_parser as fixed_parser,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    destroy_session,
    file_sha256,
    load_manifest,
    parse_positive_money,
    session_from_arguments,
    verify_destroyed,
    write_command_log,
    write_manifest,
)
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
)
from trackrelay.operator_status import (
    operator_failure,
    operator_status,
    status_activity,
)


class AwsElasticTreatmentError(RuntimeError):
    """A refused or unsuccessful elastic treatment."""


class AwsElasticTreatmentCleanupError(RuntimeError):
    """Preserve both the treatment outcome and every cleanup failure."""

    def __init__(
        self,
        *,
        workflow_error: BaseException | None,
        cleanup_errors: Sequence[BaseException],
    ) -> None:
        super().__init__("Stage 9.7 cleanup did not complete; inspect session evidence")
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


class ElasticTreatmentContract(BaseModel):
    """Pre-data bounds; these do not change the shared workload or SLOs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    maximum_backlog_events: Literal[1500] = 1500
    maximum_oldest_message_age_seconds: Literal[180] = 180
    maximum_observation_gap_seconds: Literal[30] = 30
    minimum_recovery_at_minimum_seconds: Literal[60] = 60


ELASTIC_TREATMENT_CONTRACT = ElasticTreatmentContract()


class ElasticTreatmentQualification(BaseModel):
    """A bounded acquisition-and-release result, not just an ingestion result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_run_id: UUID
    common: ElasticityGuardrailQualification
    maximum_observed_workers: int = Field(ge=0)
    maximum_outstanding_events: int = Field(ge=0)
    maximum_native_queue_work: float = Field(ge=0)
    maximum_oldest_message_age_seconds: float = Field(ge=0)
    scale_out_observed: bool
    returned_to_minimum_during_recovery: bool
    rejection_reasons: tuple[str, ...]
    qualified: bool

    @model_validator(mode="after")
    def require_consistent_decision(self) -> "ElasticTreatmentQualification":
        if self.test_run_id != self.common.test_run_id:
            raise ValueError("elastic guardrails use different run IDs")
        if self.qualified is bool(self.rejection_reasons):
            raise ValueError("elastic qualification disagrees with reasons")
        if not set(self.common.rejection_reasons).issubset(self.rejection_reasons):
            raise ValueError("elastic qualification omitted common guardrails")
        return self


def evaluate_elastic_treatment(
    result: ElasticityResult,
    cloudwatch: ElasticityCloudWatchEvidence,
    *,
    policy: WorkerAutoscalingPolicy,
    contract: ElasticTreatmentContract = ELASTIC_TREATMENT_CONTRACT,
) -> ElasticTreatmentQualification:
    """Require bounded 1-to-B-to-1 behavior within the unchanged waveform."""
    common = evaluate_treatment_guardrails(result, cloudwatch)
    reasons = list(common.rejection_reasons)
    observations = result.observations
    definition = result.definition
    duration = definition.duration_seconds
    recovery_start = duration - definition.steps[-1].duration_seconds
    minimum, maximum = policy.minimum_capacity, policy.maximum_capacity
    native = cloudwatch.series_by_id()
    worker_points = native["worker_running_tasks"].datapoints

    def at_minimum(item) -> bool:
        return (
            item.worker_desired_count == minimum
            and item.worker_running_count == minimum
            and item.worker_pending_count == 0
        )

    load_observations = tuple(
        item for item in observations if 0 <= item.seconds_after_load_started < duration
    )
    if not observations or not at_minimum(observations[0]):
        reasons.append("initial_worker_capacity_not_minimum")
    if any(
        not minimum <= item.worker_desired_count <= maximum
        or not minimum <= item.worker_running_count <= maximum
        or item.worker_pending_count > maximum
        for item in observations
    ) or any(not minimum <= point.value <= maximum for point in worker_points):
        reasons.append("worker_capacity_out_of_bounds")
    scale_out = any(
        item.worker_running_count > minimum
        and item.worker_desired_count == maximum
        and item.seconds_after_load_started < recovery_start
        for item in load_observations
    ) and any(
        point.value > minimum
        and point.interval_started_at
        < result.load_started_at + timedelta(seconds=recovery_start)
        for point in worker_points
    )
    if not scale_out:
        reasons.append("scale_out_not_observed_during_demand")

    # Require a continuously observed minimum-capacity suffix during recovery.
    # A return observed only after the load ends does not count as elasticity.
    suffix = []
    for item in reversed(load_observations):
        if item.seconds_after_load_started < recovery_start or not at_minimum(item):
            break
        suffix.append(item)
    native_recovery = tuple(
        point
        for point in worker_points
        if point.interval_started_at
        >= result.load_started_at + timedelta(seconds=recovery_start)
        and point.interval_started_at + timedelta(seconds=60)
        <= result.load_started_at + timedelta(seconds=duration)
    )
    recovered = (
        len(suffix) >= 2
        and suffix[0].seconds_after_load_started - suffix[-1].seconds_after_load_started
        >= contract.minimum_recovery_at_minimum_seconds
        and duration - suffix[0].seconds_after_load_started
        <= contract.maximum_observation_gap_seconds
        and bool(native_recovery)
        and abs(native_recovery[-1].value - minimum) <= 0.01
        and bool(observations)
        and at_minimum(observations[-1])
    )
    if not recovered:
        reasons.append("worker_recovery_not_observed_during_load")
    if (
        not load_observations
        or load_observations[0].seconds_after_load_started
        > contract.maximum_observation_gap_seconds
        or duration - load_observations[-1].seconds_after_load_started
        > contract.maximum_observation_gap_seconds
        or any(
            (later.observed_at - earlier.observed_at).total_seconds()
            > contract.maximum_observation_gap_seconds
            for earlier, later in pairwise(observations)
        )
    ):
        reasons.append("observation_coverage_incomplete")
    if (result.load_ended_at - result.load_started_at).total_seconds() < duration:
        reasons.append("workload_window_incomplete")
    if any(item.dead_letter_queue_messages for item in observations):
        reasons.append("observed_dead_letter_queue_not_empty")
    outstanding = max(
        (
            max(0, item.database_persisted_events - item.completed_delivery_events)
            for item in observations
        ),
        default=0,
    )
    native_queue_work = max(
        sum(
            points[index].value
            for points in (
                native["source_queue_visible"].datapoints,
                native["source_queue_in_flight"].datapoints,
                native["source_queue_delayed"].datapoints,
            )
        )
        for index in range(len(worker_points))
    )
    oldest_age = max(
        point.value for point in native["source_queue_oldest_age"].datapoints
    )
    if (
        max(outstanding, result.maximum_source_queue_work, native_queue_work)
        > contract.maximum_backlog_events
    ):
        reasons.append("backlog_bound_exceeded")
    if oldest_age > contract.maximum_oldest_message_age_seconds:
        reasons.append("oldest_message_age_exceeded")
    return ElasticTreatmentQualification(
        test_run_id=result.test_run_id,
        common=common,
        maximum_observed_workers=max(
            (item.worker_running_count for item in observations), default=0
        ),
        maximum_outstanding_events=outstanding,
        maximum_native_queue_work=native_queue_work,
        maximum_oldest_message_age_seconds=oldest_age,
        scale_out_observed=scale_out,
        returned_to_minimum_during_recovery=recovered,
        rejection_reasons=tuple(reasons),
        qualified=not reasons,
    )


class ElasticTreatmentSummary(BaseModel):
    """Self-contained elastic evidence linked to the qualified fixed run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    fixed_test_run_id: UUID
    policy: WorkerAutoscalingPolicy
    contract: ElasticTreatmentContract
    measurement: ElasticityResult
    cloudwatch: ElasticityCloudWatchEvidence
    qualification: ElasticTreatmentQualification

    @model_validator(mode="after")
    def require_reproducible_decision(self) -> "ElasticTreatmentSummary":
        if self.fixed_test_run_id == self.measurement.test_run_id:
            raise ValueError("elastic replay must use a new run ID")
        if self.qualification != evaluate_elastic_treatment(
            self.measurement,
            self.cloudwatch,
            policy=self.policy,
            contract=self.contract,
        ):
            raise ValueError("elastic summary decision differs from its evidence")
        return self


def validate_elastic_treatment_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> dict[str, object]:
    """Refuse unapproved commands before any workload or teardown action."""
    if (
        not session.session_id.startswith("cloud-session-4-")
        or session.deployment_mode != "async"
    ):
        raise AwsSessionError("Stage 9.7 requires async cloud session 4")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "worker_autoscaling_verified":
        raise AwsSessionError("elastic treatment requires verified worker autoscaling")
    recorded = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded, str) or parse_positive_money(
        approved_cost_ceiling_usd, field_name="approved cost ceiling"
    ) != parse_positive_money(recorded, field_name="recorded cost ceiling"):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    return manifest


def _load_prerequisites(
    session: AwsSession, manifest: dict[str, object]
) -> tuple[FixedControlSummary, ElasticityTransitionEvidence]:
    for key, field, expected_path in (
        ("fixed_control", "summary", "elasticity/fixed/summary.json"),
        (
            "worker_autoscaling_transition",
            "evidence",
            "elasticity/transition/evidence.json",
        ),
    ):
        item = manifest.get(key)
        if not isinstance(item, dict) or item.get(field) != expected_path:
            raise AwsElasticTreatmentError(
                "required fixed/transition evidence is not recorded"
            )
    try:
        fixed = FixedControlSummary.model_validate_json(
            (session.evidence_dir / "elasticity/fixed/summary.json").read_text(
                encoding="utf-8"
            )
        )
        transition = ElasticityTransitionEvidence.model_validate_json(
            (session.evidence_dir / "elasticity/transition/evidence.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError) as error:
        raise AwsElasticTreatmentError(
            "fixed/transition evidence is invalid"
        ) from error
    if (
        not fixed.qualification.qualified
        or fixed.qualification
        != evaluate_fixed_control_qualification(fixed.measurement, fixed.cloudwatch)
        or fixed.measurement.definition != ELASTICITY_WORKLOAD_DEFINITION
        or fixed.measurement.test_run_id != transition.reset_fixed_test_run_id
    ):
        raise AwsElasticTreatmentError(
            "fixed control, workload, or reset identity changed"
        )
    return fixed, transition


def verify_elastic_environment(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    policy: WorkerAutoscalingPolicy,
    api_url: str,
    source_queue_url: str,
    dead_letter_queue_url: str,
    evidence_root: Path,
    phase: Literal["before", "after"],
    runner: ProcessRunner,
    policy_verifier: Callable[
        ..., WorkerAutoscalingVerification
    ] = verify_native_worker_autoscaling,
    client: httpx.Client | None = None,
) -> None:
    """Verify policy and no configuration drift; never apply infrastructure."""
    require_clean_approved_revision(session, runner=runner)
    output_policy = WorkerAutoscalingPolicy.model_validate(
        terraform_output(
            session, "worker_autoscaling_policy", runner=runner, json_output=True
        )
    )
    if (
        output_policy != policy
        or source_queue_url.rsplit("/", 1)[-1] != policy.queue_name
    ):
        raise AwsElasticTreatmentError("elastic policy or source queue changed")
    plan = evidence_root / f"{phase}-unchanged.tfplan"
    planned = runner(
        session.terraform_command(
            "plan",
            "-input=false",
            "-detailed-exitcode",
            f"-out={plan.resolve()}",
            *_terraform_variables(session, image_digests=_image_digests(manifest)),
        ),
        None,
    )
    write_command_log(evidence_root / f"{phase}-plan.log", planned)
    if planned.returncode != 0:
        raise AwsElasticTreatmentError(
            "elastic environment has configuration drift or plan failed"
        )
    native = policy_verifier(session, expected=policy, runner=runner)
    if not native.matches(policy):
        raise AwsElasticTreatmentError("native worker autoscaling changed")
    (evidence_root / f"{phase}-environment.json").write_text(
        native.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (evidence_root / f"{phase}-plan.sha256").write_text(
        file_sha256(plan) + "\n", encoding="utf-8"
    )
    _, cluster_name, worker_service_name = policy.resource_id.split("/")
    for role, desired in (("api", 2), ("simulator", 1)):
        service = worker_service_name.removesuffix("-worker") + f"-{role}"
        if _worker_counts(
            session,
            cluster_name=cluster_name,
            worker_service_name=service,
            runner=runner,
        ) != (desired, desired, 0):
            raise AwsElasticTreatmentError("non-worker service capacity is not stable")
    if phase == "before":
        owned_client = client is None
        client = client or httpx.Client(base_url=api_url, timeout=10)
        try:
            observation = _transition_observation(
                session,
                client=client,
                source_queue_url=source_queue_url,
                dead_letter_queue_url=dead_letter_queue_url,
                cluster_name=cluster_name,
                worker_service_name=worker_service_name,
                runner=runner,
                observed_at=datetime.now(UTC),
            )
            (evidence_root / "pre-load.json").write_text(
                observation.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            if not observation.empty_at_minimum:
                raise AwsElasticTreatmentError(
                    "elastic replay did not start empty at one worker"
                )
        finally:
            if owned_client:
                client.close()


def run_elastic_treatment_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    treatment_runner: Callable[..., ElasticityResult] = execute_elasticity_workload,
    metric_collector: Callable[
        ..., ElasticityCloudWatchEvidence
    ] = collect_elasticity_cloudwatch_evidence,
    environment_verifier: Callable[..., None] = verify_elastic_environment,
    runner: ProcessRunner = run_process,
    destroyer: Callable[..., object] = destroy_session,
    teardown_verifier: Callable[..., object] = verify_destroyed,
) -> ElasticTreatmentSummary:
    """Retain both results, then destroy and verify on success, failure, or interrupt."""
    manifest = validate_elastic_treatment_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=approved_unconditional_teardown_session_id,
    )
    workflow_error: BaseException | None = None
    try:
        fixed, transition = _load_prerequisites(session, manifest)
        api_url, source_url, dlq_url, cluster, worker, root = _prepare_fixed_control(
            session,
            manifest=manifest,
            definition=fixed.measurement.definition,
            runner=runner,
            treatment=ElasticityTreatment.ELASTIC,
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
            "Elastic treatment: verifying unchanged pre-load environment"
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
            definition=fixed.measurement.definition,
            runner=runner,
        )
        if (
            result.definition != fixed.measurement.definition
            or result.load_started_at < transition.verified_at
            or result.test_run_id == fixed.measurement.test_run_id
        ):
            raise AwsElasticTreatmentError(
                "elastic workload, run identity, or timing changed"
            )
        with status_activity("Elastic treatment: collecting CloudWatch evidence"):
            cloudwatch = metric_collector(
                session,
                test_run_id=result.test_run_id,
                window_started_at=result.load_started_at,
                window_ended_at=result.load_ended_at,
                runner=runner,
            )
        summary = ElasticTreatmentSummary(
            fixed_test_run_id=fixed.measurement.test_run_id,
            policy=transition.policy,
            contract=contract,
            measurement=result,
            cloudwatch=cloudwatch,
            qualification=evaluate_elastic_treatment(
                result, cloudwatch, policy=transition.policy, contract=contract
            ),
        )
        (root / "cloudwatch.json").write_text(
            cloudwatch.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (root / "summary.json").write_text(
            summary.model_dump_json(indent=2, round_trip=True) + "\n", encoding="utf-8"
        )
        with status_activity(
            "Elastic treatment: verifying unchanged post-load environment"
        ):
            environment_verifier(session, phase="after", **environment_arguments)
        manifest = load_manifest(session)
        manifest["elastic_treatment"] = {
            **manifest["elastic_treatment"],
            "fixed_test_run_id": str(summary.fixed_test_run_id),
            "test_run_id": str(result.test_run_id),
            "summary": "elasticity/elastic/summary.json",
            "qualified": summary.qualification.qualified,
        }
        manifest["status"] = (
            "elastic_treatment_qualified"
            if summary.qualification.qualified
            else "elastic_treatment_rejected"
        )
        write_manifest(session, manifest)
        if not summary.qualification.qualified:
            raise AwsElasticTreatmentError(
                "elastic treatment rejected: "
                + ", ".join(summary.qualification.rejection_reasons)
            )
        operator_status("Elastic treatment qualified; beginning unconditional teardown")
        return summary
    except BaseException as error:
        workflow_error = error
        operator_failure("Phase elastic; beginning cleanup", error)
        raise
    finally:
        cleanup_errors = []
        for action in (destroyer, teardown_verifier):
            try:
                action(session)
            except BaseException as error:  # noqa: BLE001 - always attempt native verification
                cleanup_errors.append(error)
        if cleanup_errors:
            raise AwsElasticTreatmentCleanupError(
                workflow_error=workflow_error, cleanup_errors=cleanup_errors
            ) from workflow_error


def run_from_arguments(arguments: Namespace) -> None:
    run_elastic_treatment_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=arguments.approved_unconditional_teardown_session_id,
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during Stage 9.7 elastic treatment")


def main(argv: Sequence[str] | None = None) -> int:
    previous = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        parser = fixed_parser()
        parser.description = __doc__
        run_from_arguments(parser.parse_args(argv))
    except (
        AwsElasticTreatmentError,
        AwsElasticTreatmentCleanupError,
        AwsFixedControlError,
        AwsAsyncDeploymentError,
        AwsElasticityTransitionError,
        AwsElasticityCloudWatchError,
        AwsSessionError,
        KeyboardInterrupt,
        httpx.HTTPError,
    ) as error:
        raise SystemExit(f"AWS elastic treatment failed: {error}") from error
    finally:
        signal(SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
