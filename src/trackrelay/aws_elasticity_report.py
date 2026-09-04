"""Build the fixed/elastic comparison offline from a torn-down session."""

from argparse import ArgumentParser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from json import loads
from pathlib import Path
from re import DOTALL, fullmatch
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from trackrelay.aws_elastic_treatment import (
    ELASTIC_TREATMENT_CONTRACT,
    ElasticTreatmentSummary,
)
from trackrelay.aws_elasticity_cloudwatch import ElasticityCloudWatchEvidence
from trackrelay.aws_elasticity_transition import (
    ElasticityTransitionEvidence,
    ElasticityTransitionObservation,
    WorkerAutoscalingVerification,
)
from trackrelay.aws_experiment_reset import ExperimentResetResult
from trackrelay.aws_fixed_control import (
    ElasticityResult,
    FixedControlObservation,
    FixedControlSummary,
    evaluate_fixed_control_qualification,
    evaluate_treatment_guardrails,
)
from trackrelay.experiments.elasticity import ELASTICITY_WORKLOAD_DEFINITION
from trackrelay.experiments.request_timings import timing_windows


class ElasticityReportError(RuntimeError):
    """Saved evidence is incomplete, inconsistent, or cannot be rendered safely."""


class StepSupportResult(BaseModel):
    """Observed completion support within one short plateau, not an extrapolation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_name: str
    offered_rate_per_second: int
    window_started_seconds: float | None
    window_ended_seconds: float | None
    completed_events_per_second: float | None
    outstanding_change: int | None
    maximum_backlog_events: float | None
    maximum_message_age_seconds: float | None
    rejection_reasons: tuple[str, ...]
    supported: bool

    @model_validator(mode="after")
    def require_consistent_support(self) -> "StepSupportResult":
        if self.supported is bool(self.rejection_reasons):
            raise ValueError("step support disagrees with rejection reasons")
        return self


class TreatmentReport(BaseModel):
    """Compact treatment result with explicit unavailable measurements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    treatment: Literal["fixed", "elastic"]
    test_run_id: UUID
    qualified: bool
    rejection_reasons: tuple[str, ...]
    highest_supported_rate_per_second: int | None
    step_results: tuple[StepSupportResult, ...]
    minimum_workers: int | None
    maximum_workers: int | None
    scale_out_seconds_after_start: float | None
    return_to_minimum_seconds_after_start: float | None
    stable_drain_seconds_after_load: float | None
    drain_confirmed_seconds_after_load: float | None
    maximum_sampled_queue_work: int
    maximum_native_oldest_age_seconds: float
    maximum_driver_step_p95_ms: float
    measurement_rejection_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def require_observed_rate(self) -> "TreatmentReport":
        rates = {step.offered_rate_per_second for step in self.step_results}
        expected = max(
            (
                rate
                for rate in rates
                if all(
                    step.supported
                    for step in self.step_results
                    if step.offered_rate_per_second == rate
                )
            ),
            default=None,
        )
        if self.highest_supported_rate_per_second != expected:
            raise ValueError("highest supported rate disagrees with step results")
        if self.qualified is bool(self.rejection_reasons):
            raise ValueError("treatment qualification disagrees with reasons")
        return self


class ElasticityComparisonReport(BaseModel):
    """A reproducible measured-waveform claim, distinct from steady-state capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method: Literal[
        "short-plateau-completion-v1",
        "short-plateau-completion-v2",
        "short-plateau-completion-v3",
    ] = "short-plateau-completion-v1"
    generated_at: AwareDatetime
    session_id: str
    region: str
    git_revision: str
    teardown_verified_at: AwareDatetime
    fixed: TreatmentReport
    elastic: TreatmentReport
    observed_step_rate_multiplier: float | None
    elasticity_demonstrated: bool
    conclusion: str
    source_sha256: dict[str, str]

    @model_validator(mode="after")
    def require_earned_comparison(self) -> "ElasticityComparisonReport":
        if (
            self.fixed.treatment != "fixed"
            or self.elastic.treatment != "elastic"
            or self.fixed.test_run_id == self.elastic.test_run_id
        ):
            raise ValueError("comparison requires distinct ordered treatments")
        demonstrated = (
            self.fixed.qualified
            and self.elastic.qualified
            and not self.fixed.measurement_rejection_reasons
            and not self.elastic.measurement_rejection_reasons
        )
        source, target = (
            self.fixed.highest_supported_rate_per_second,
            self.elastic.highest_supported_rate_per_second,
        )
        expected = target / source if demonstrated and source and target else None
        if (
            self.elasticity_demonstrated != demonstrated
            or self.observed_step_rate_multiplier != expected
        ):
            raise ValueError("comparison claim differs from observed results")
        return self


MAXIMUM_GAP_SECONDS = ELASTIC_TREATMENT_CONTRACT.maximum_observation_gap_seconds
STABLE_DRAIN_SECONDS = 180
DRAIN_DEADLINE_SECONDS = 1200


def _outstanding(item: FixedControlObservation) -> int:
    return item.database_persisted_events - item.completed_delivery_events


def _stable_drain(result: ElasticityResult) -> tuple[float | None, float | None]:
    suffix: list[FixedControlObservation] = []
    for item in reversed(result.observations):
        if (
            item.observed_at < result.load_ended_at
            or not item.processing_drained
            or item.completed_delivery_events
            != result.definition.expected_request_count
            or (
                suffix
                and (suffix[-1].observed_at - item.observed_at).total_seconds()
                > MAXIMUM_GAP_SECONDS
            )
        ):
            break
        suffix.append(item)
    suffix.reverse()
    if not suffix or not result.drain_stability_confirmed:
        return None, None
    first = suffix[0]
    confirmed = next(
        (
            item
            for item in suffix
            if (item.observed_at - first.observed_at).total_seconds()
            >= STABLE_DRAIN_SECONDS
        ),
        None,
    )
    if (
        confirmed is None
        or (confirmed.observed_at - result.load_ended_at).total_seconds()
        > DRAIN_DEADLINE_SECONDS
    ):
        return None, None
    return (
        (first.observed_at - result.load_ended_at).total_seconds(),
        (confirmed.observed_at - result.load_ended_at).total_seconds(),
    )


def _validate_measurement(result: ElasticityResult) -> tuple[str, ...]:
    """Reject contradictory counters; treat ordinary collection gaps as unavailable."""
    for step, measured in zip(
        result.definition.steps, result.ingestion_steps, strict=True
    ):
        if (
            step.offered_rate_per_second != measured.offered_rate_per_second
            or step.duration_seconds != measured.duration_seconds
            or step.expected_request_count != measured.expected_request_count
        ):
            raise ElasticityReportError(
                "saved ingestion step differs from its workload"
            )
    for item in result.observations:
        elapsed = (item.observed_at - result.load_started_at).total_seconds()
        if abs(elapsed - item.seconds_after_load_started) > 0.001 or elapsed < 0:
            raise ElasticityReportError(
                "observation elapsed time disagrees with UTC timestamp"
            )
        if (
            _outstanding(item) < 0
            or item.database_persisted_events > result.definition.expected_request_count
        ):
            raise ElasticityReportError("observation has impossible delivery counters")
    if any(
        later.database_persisted_events < earlier.database_persisted_events
        or later.completed_delivery_events < earlier.completed_delivery_events
        for earlier, later in pairwise(result.observations)
    ):
        raise ElasticityReportError(
            "cumulative event counters decreased during treatment"
        )
    reasons = []
    duration = result.definition.duration_seconds
    if not result.observations or (
        result.observations[0].seconds_after_load_started > MAXIMUM_GAP_SECONDS
        or result.observations[-1].seconds_after_load_started < duration
        or any(
            (b.observed_at - a.observed_at).total_seconds() > MAXIMUM_GAP_SECONDS
            for a, b in pairwise(result.observations)
        )
        or result.observation_failures
    ):
        reasons.append("observation_coverage_incomplete")
    if (result.load_ended_at - result.load_started_at).total_seconds() < duration:
        reasons.append("workload_window_incomplete")
    if _stable_drain(result)[0] is None:
        reasons.append("stable_drain_not_established")
    return tuple(reasons)


def _step_results(
    result: ElasticityResult,
    native: ElasticityCloudWatchEvidence,
    global_reasons: tuple[str, ...],
) -> tuple[StepSupportResult, ...]:
    series = native.series_by_id()
    reports = []
    start = 0
    for step, ingestion in zip(
        result.definition.steps, result.ingestion_steps, strict=True
    ):
        end = start + step.duration_seconds
        samples = tuple(
            item
            for item in result.observations
            if start <= item.seconds_after_load_started <= end
        )
        # Keep actual native buckets. Edge overlap is conservative, not relabelled.
        indices = tuple(
            index
            for index, point in enumerate(series["source_queue_visible"].datapoints)
            if point.interval_started_at
            < result.load_started_at + timedelta(seconds=end)
            and point.interval_started_at + timedelta(seconds=60)
            > result.load_started_at + timedelta(seconds=start)
        )
        native_max = max(
            (
                sum(
                    series[key].datapoints[index].value
                    for key in (
                        "source_queue_visible",
                        "source_queue_in_flight",
                        "source_queue_delayed",
                    )
                )
                for index in indices
            ),
            default=None,
        )
        age = max(
            (
                series["source_queue_oldest_age"].datapoints[index].value
                for index in indices
            ),
            default=None,
        )
        reasons = list(global_reasons)
        if not indices:
            reasons.append("native_step_window_incomplete")
        completion_rate = None
        change = None
        backlog = None
        first, last = (samples[0], samples[-1]) if samples else (None, None)
        if (
            len(samples) < 2
            or first.seconds_after_load_started - start > MAXIMUM_GAP_SECONDS
            or end - last.seconds_after_load_started > MAXIMUM_GAP_SECONDS
            or last.seconds_after_load_started - first.seconds_after_load_started
            < max(
                10 if result.definition.uses_request_timings else 30,
                step.duration_seconds - 2 * MAXIMUM_GAP_SECONDS,
            )
        ):
            reasons.append("step_window_incomplete")
        else:
            elapsed = last.seconds_after_load_started - first.seconds_after_load_started
            completion_rate = (
                last.completed_delivery_events - first.completed_delivery_events
            ) / elapsed
            change = _outstanding(last) - _outstanding(first)
            backlog = max(
                *([native_max] if native_max is not None else []),
                *(max(item.source_queue_work, _outstanding(item)) for item in samples),
            )
            if completion_rate + 1e-9 < step.offered_rate_per_second:
                reasons.append("completion_rate_below_offered_rate")
            if change > 0:
                reasons.append("outstanding_backlog_grew")
            if backlog > ELASTIC_TREATMENT_CONTRACT.maximum_backlog_events:
                reasons.append("backlog_bound_exceeded")
        if (
            age is not None
            and age > ELASTIC_TREATMENT_CONTRACT.maximum_oldest_message_age_seconds
        ):
            reasons.append("oldest_message_age_exceeded")
        if not ingestion.ingestion_guardrails_passed:
            reasons.append("step_ingestion_failed")
        if any(item.dead_letter_queue_messages for item in samples):
            reasons.append("observed_dead_letter_queue_not_empty")
        reports.append(
            StepSupportResult(
                step_name=step.name,
                offered_rate_per_second=step.offered_rate_per_second,
                window_started_seconds=first.seconds_after_load_started
                if first
                else None,
                window_ended_seconds=last.seconds_after_load_started if last else None,
                completed_events_per_second=completion_rate,
                outstanding_change=change,
                maximum_backlog_events=backlog,
                maximum_message_age_seconds=age,
                rejection_reasons=tuple(dict.fromkeys(reasons)),
                supported=not reasons,
            )
        )
        start = end
    return tuple(reports)


def summarize_treatment(
    summary: FixedControlSummary | ElasticTreatmentSummary,
) -> TreatmentReport:
    """Apply identical observed-step rules without treating transient bursts as capacity."""
    fixed = isinstance(summary, FixedControlSummary)
    result = summary.measurement
    measurement_reasons = _validate_measurement(result)
    common = evaluate_treatment_guardrails(result, summary.cloudwatch)
    steps = _step_results(
        result, summary.cloudwatch, (*measurement_reasons, *common.rejection_reasons)
    )
    rates = {step.offered_rate_per_second for step in steps}
    supported_rates = [
        rate
        for rate in rates
        if all(step.supported for step in steps if step.offered_rate_per_second == rate)
    ]
    observations = result.observations
    expanded = next(
        (item for item in observations if item.worker_running_count > 1), None
    )
    suffix = []
    recovery_start = (
        result.definition.duration_seconds
        - result.definition.steps[-1].duration_seconds
    )
    for item in reversed(observations):
        if (
            item.worker_running_count != 1
            or item.worker_desired_count != 1
            or item.worker_pending_count
            or item.seconds_after_load_started < recovery_start
        ):
            break
        suffix.append(item)
    return_seconds = None
    if not fixed and expanded is not None and suffix and not measurement_reasons:
        first_return = suffix[-1].seconds_after_load_started
        if (
            result.definition.duration_seconds - first_return
            >= ELASTIC_TREATMENT_CONTRACT.minimum_recovery_at_minimum_seconds
        ):
            return_seconds = first_return
    drained, confirmed = _stable_drain(result)
    return TreatmentReport(
        treatment="fixed" if fixed else "elastic",
        test_run_id=result.test_run_id,
        qualified=summary.qualification.qualified,
        rejection_reasons=summary.qualification.rejection_reasons,
        highest_supported_rate_per_second=max(supported_rates, default=None),
        step_results=steps,
        minimum_workers=min(
            (item.worker_running_count for item in observations), default=None
        ),
        maximum_workers=max(
            (item.worker_running_count for item in observations), default=None
        ),
        scale_out_seconds_after_start=expanded.seconds_after_load_started
        if expanded
        else None,
        return_to_minimum_seconds_after_start=return_seconds,
        stable_drain_seconds_after_load=drained,
        drain_confirmed_seconds_after_load=confirmed,
        maximum_sampled_queue_work=result.maximum_source_queue_work,
        maximum_native_oldest_age_seconds=max(
            point.value
            for point in summary.cloudwatch.series_by_id()[
                "source_queue_oldest_age"
            ].datapoints
        ),
        maximum_driver_step_p95_ms=max(
            step.p95_response_latency_ms for step in result.ingestion_steps
        ),
        measurement_rejection_reasons=measurement_reasons,
    )


def build_comparison(
    fixed: FixedControlSummary,
    elastic: ElasticTreatmentSummary,
    *,
    session_id: str,
    region: str,
    git_revision: str,
    teardown_verified_at: datetime,
    source_sha256: dict[str, str],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ElasticityComparisonReport:
    """Recompute qualifications and write only conclusions supported by both runs."""
    if (
        fixed.measurement.test_run_id != elastic.fixed_test_run_id
        or fixed.measurement.definition != elastic.measurement.definition
        or fixed.measurement.definition != ELASTICITY_WORKLOAD_DEFINITION
        or fixed.measurement.load_ended_at >= elastic.measurement.load_started_at
    ):
        raise ElasticityReportError(
            "treatments do not form the unchanged ordered comparison"
        )
    if fixed.qualification != evaluate_fixed_control_qualification(
        fixed.measurement, fixed.cloudwatch
    ):
        raise ElasticityReportError(
            "saved fixed qualification differs from its evidence"
        )
    if not fixed.qualification.qualified:
        raise ElasticityReportError(
            "a rejected fixed candidate cannot support the comparison"
        )
    # Round-trip also verifies elastic decisions when passed model_copy test objects.
    elastic = ElasticTreatmentSummary.model_validate_json(
        elastic.model_dump_json(round_trip=True)
    )
    source, target = summarize_treatment(fixed), summarize_treatment(elastic)
    demonstrated = (
        source.qualified
        and target.qualified
        and not source.measurement_rejection_reasons
        and not target.measurement_rejection_reasons
    )
    source_rate, target_rate = (
        source.highest_supported_rate_per_second,
        target.highest_supported_rate_per_second,
    )
    multiplier = (
        target_rate / source_rate
        if demonstrated and source_rate and target_rate
        else None
    )
    conclusion = (
        f"On the same {fixed.measurement.definition.peak_rate_per_second}-events/s peak waveform, "
        f"workers expanded from 1 to {target.maximum_workers} and returned to 1 during recovery, "
        "with bounded backlog and all treatment guardrails passing."
        if demonstrated
        else "This evidence does not establish the complete worker-elasticity claim; review the failed or missing guardrails below."
    )
    if multiplier is not None:
        conclusion += f" The highest supported short-step rate changed from {source_rate} to {target_rate} events/s ({multiplier:.2f}x)."
    else:
        conclusion += " A supported-step rate multiplier is not established."
    return ElasticityComparisonReport(
        method=(
            "short-plateau-completion-v3"
            if fixed.measurement.definition.name == "aws-elasticity-demo-v6"
            else "short-plateau-completion-v2"
            if fixed.measurement.definition.uses_request_timings
            else "short-plateau-completion-v1"
        ),
        generated_at=now(),
        session_id=session_id,
        region=region,
        git_revision=git_revision,
        teardown_verified_at=teardown_verified_at,
        fixed=source,
        elastic=target,
        observed_step_rate_multiplier=multiplier,
        elasticity_demonstrated=demonstrated,
        conclusion=conclusion,
        source_sha256=source_sha256,
    )


# Match all native resource families checked by the session teardown controller.
REQUIRED_NATIVE_INVENTORY = frozenset(
    {
        "ec2_instances",
        "ebs_volumes",
        "internet_gateways",
        "route_tables",
        "security_groups",
        "subnets",
        "vpcs",
        "application_load_balancers",
        "load_balancer_target_groups",
        "ecs_clusters",
        "ecs_services",
        "ecs_tasks",
        "active_ecs_task_definitions",
        "service_discovery_namespaces",
        "cloudwatch_log_groups",
        "container_insights_log_groups",
        "cloudwatch_dashboards",
        "application_autoscaling_targets",
        "application_autoscaling_policies",
        "cloudwatch_metric_alarms",
        "ecr_repositories",
        "sqs_queues",
        "iam_instance_profiles",
        "iam_roles",
        "rds_instances",
        "rds_automated_backups",
        "rds_manual_snapshots",
        "rds_subnet_groups",
        "rds_parameter_groups",
        "rds_managed_secrets",
    }
)


class SavedEvidence:
    """Read only known in-session files, retaining provenance without private payloads."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.hashes: dict[str, str] = {}

    def read(self, relative: str) -> bytes:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ElasticityReportError("evidence path escapes the session directory")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ElasticityReportError(
                f"missing or unreadable evidence: {relative}"
            ) from error
        self.hashes[relative] = sha256(content).hexdigest()
        return content

    def model[T: BaseModel](self, relative: str, model: type[T]) -> T:
        try:
            return model.model_validate_json(self.read(relative))
        except ValueError as error:
            raise ElasticityReportError(
                f"invalid saved evidence: {relative}"
            ) from error

    def json(self, relative: str) -> dict:
        try:
            document = loads(self.read(relative))
        except ValueError as error:
            raise ElasticityReportError(f"invalid JSON evidence: {relative}") from error
        if not isinstance(document, dict):
            raise ElasticityReportError(f"expected a JSON object: {relative}")
        return document

    def log(self, relative: str) -> tuple[int, str, str]:
        match = fullmatch(
            r"exit_code: (-?\d+)\n\nstdout:\n(.*?)\n\nstderr:\n(.*)",
            self.read(relative).decode("utf-8"),
            DOTALL,
        )
        if match is None:
            raise ElasticityReportError(f"invalid saved command log: {relative}")
        return int(match[1]), match[2], match[3]


def load_comparison_evidence(root: Path):
    """Verify the completed local evidence chain without AWS, Terraform, or Git calls."""
    saved = SavedEvidence(root)
    manifest = saved.json("session.json")
    if "elasticity_diagnostic_session" in manifest or "elastic_diagnostic" in manifest:
        raise ElasticityReportError(
            "elastic-only diagnostic evidence cannot produce a paired comparison"
        )
    if (
        manifest.get("status") != "teardown_verified"
        or manifest.get("deployment_mode") != "async"
        or not isinstance(manifest.get("session_id"), str)
        or fullmatch(r"cloud-session-4-\d{8}T\d{6}Z", manifest["session_id"]) is None
        or fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_revision", ""))) is None
        or not isinstance(manifest.get("region"), str)
    ):
        raise ElasticityReportError(
            "report requires a verified, torn-down async session 4"
        )
    try:
        verified = datetime.fromisoformat(manifest["teardown_verified_at"])
        if verified.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ElasticityReportError(
            "teardown verification timestamp is invalid"
        ) from error
    inventory = saved.json("aws-native-inventory-after-destroy.json")
    if not REQUIRED_NATIVE_INVENTORY.issubset(inventory) or any(
        type(count) is not int or count != 0 for count in inventory.values()
    ):
        raise ElasticityReportError(
            "native teardown inventory is incomplete or nonempty"
        )
    code, stdout, stderr = saved.log("terraform-state-after-destroy.log")
    if stdout.strip() or (code != 0 and "No state file was found!" not in stderr):
        raise ElasticityReportError("Terraform teardown state was not verified empty")
    fixed = saved.model("elasticity/fixed/summary.json", FixedControlSummary)
    elastic = saved.model("elasticity/elastic/summary.json", ElasticTreatmentSummary)
    reset = saved.model("elasticity/reset/result.json", ExperimentResetResult)
    transition = saved.model(
        "elasticity/transition/evidence.json", ElasticityTransitionEvidence
    )
    for name, field, expected in (
        ("fixed_control", "summary", "elasticity/fixed/summary.json"),
        ("elastic_treatment", "summary", "elasticity/elastic/summary.json"),
        (
            "worker_autoscaling_transition",
            "evidence",
            "elasticity/transition/evidence.json",
        ),
        ("experiment_reset", "result", "elasticity/reset/result.json"),
    ):
        entry = manifest.get(name)
        if (
            not isinstance(entry, dict)
            or entry.get(field) != expected
            or entry.get("git_revision") != manifest["git_revision"]
        ):
            raise ElasticityReportError(
                "session journal does not reference the expected evidence"
            )
    if (
        manifest["elastic_treatment"].get("qualified")
        is not elastic.qualification.qualified
        or reset.fixed_test_run_id != fixed.measurement.test_run_id
        or transition.reset_fixed_test_run_id != fixed.measurement.test_run_id
        or transition.policy != elastic.policy
        or reset.application_reset.expected_event_count
        != fixed.measurement.definition.expected_request_count
        or reset.started_at
        < max(
            (item.observed_at for item in fixed.measurement.observations),
            default=fixed.measurement.load_ended_at,
        )
        or transition.applied_at < reset.completed_at
        or elastic.measurement.load_started_at < transition.verified_at
        or verified
        < max(
            elastic.cloudwatch.collected_at,
            max(
                (item.observed_at for item in elastic.measurement.observations),
                default=elastic.measurement.load_ended_at,
            ),
        )
    ):
        raise ElasticityReportError(
            "reset, policy, timing, or treatment journal is inconsistent"
        )
    for phase in ("before", "after"):
        native = saved.model(
            f"elasticity/elastic/{phase}-environment.json",
            WorkerAutoscalingVerification,
        )
        if (
            not native.matches(elastic.policy)
            or native.collected_at > verified
            or (
                phase == "before"
                and native.collected_at > elastic.measurement.load_started_at
            )
            or (
                phase == "after"
                and native.collected_at < elastic.measurement.load_ended_at
            )
        ):
            raise ElasticityReportError(
                "saved native policy differs from the treatment"
            )
        plan = saved.read(f"elasticity/elastic/{phase}-unchanged.tfplan")
        digest = (
            saved.read(f"elasticity/elastic/{phase}-plan.sha256")
            .decode("utf-8")
            .strip()
        )
        if (
            sha256(plan).hexdigest() != digest
            or saved.log(f"elasticity/elastic/{phase}-plan.log")[0] != 0
        ):
            raise ElasticityReportError("saved no-change environment plan is invalid")
    pre_load = saved.model(
        "elasticity/elastic/pre-load.json", ElasticityTransitionObservation
    )
    if (
        not pre_load.empty_at_minimum
        or not transition.verified_at
        <= pre_load.observed_at
        <= elastic.measurement.load_started_at
    ):
        raise ElasticityReportError(
            "elastic pre-load state is not a verified empty boundary"
        )
    return fixed, elastic, manifest, verified, saved.hashes


def render_comparison_figure(
    report: ElasticityComparisonReport,
    fixed: FixedControlSummary,
    elastic: ElasticTreatmentSummary,
    output: Path,
) -> None:
    """Render actual samples and native buckets on aligned axes, using headless Agg."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(14, 11), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(4, 2, sharex=True, sharey="row")
    summaries = (fixed, elastic)
    end_seconds = max(
        max(
            (
                point.seconds_after_load_started
                for point in item.measurement.observations
            ),
            default=item.measurement.definition.duration_seconds,
        )
        for item in summaries
    )
    colors = ("#42628d", "#007e80")
    for column, (summary, color) in enumerate(zip(summaries, colors, strict=True)):
        result = summary.measurement
        duration = result.definition.duration_seconds
        recovery = duration - result.definition.steps[-1].duration_seconds
        x, rates, elapsed = [0], [], 0
        for step in result.definition.steps:
            rates.append(step.offered_rate_per_second)
            elapsed += step.duration_seconds
            x.append(elapsed)
        axes[0, column].stairs([*rates, 0], [*x, end_seconds], color=color, linewidth=2)
        axes[0, column].set_title(
            "Fixed • one worker" if column == 0 else "Elastic • bounded 1–8 workers",
            loc="left",
            fontweight="bold",
        )
        points = result.observations

        def sampled(values, points=points):
            times, observed = [], []
            previous = None
            for item, value in zip(points, values, strict=True):
                if (
                    previous is not None
                    and item.seconds_after_load_started - previous > MAXIMUM_GAP_SECONDS
                ):
                    times.append(float("nan"))
                    observed.append(float("nan"))
                times.append(item.seconds_after_load_started)
                observed.append(value)
                previous = item.seconds_after_load_started
            return times, observed

        axes[1, column].step(
            *sampled([p.worker_running_count for p in points]),
            where="post",
            color=color,
            label="Observed running",
        )
        workers = summary.cloudwatch.series_by_id()["worker_running_tasks"].datapoints
        axes[1, column].plot(
            [
                (p.interval_started_at - result.load_started_at).total_seconds()
                for p in workers
            ],
            [p.value for p in workers],
            linestyle="none",
            marker=".",
            color="#6c737f",
            label="Native 60s average",
        )
        axes[2, column].plot(
            *sampled([p.source_queue_work for p in points]),
            color=color,
            label="SQS work (sampled)",
        )
        axes[2, column].plot(
            *sampled([_outstanding(p) for p in points]),
            color="#866045",
            linestyle="--",
            label="Accepted, not delivered",
        )
        latency = summary.cloudwatch.series_by_id()["alb_p95_latency"].datapoints
        windows = timing_windows(result.request_timings, result.load_started_at)
        if windows:
            axes[3, column].plot(
                [w["seconds_after_start"] for w in windows],
                [w["p95_ms"] for w in windows],
                color="#222222",
                label="Ingestion p95 / 10s (raw samples)",
            )
        for index, point in enumerate(latency):
            start = (point.interval_started_at - result.load_started_at).total_seconds()
            axes[3, column].hlines(
                1000 * point.value,
                start,
                start + 60,
                color=color,
                linewidth=2,
                label="ALB native 60s p95" if index == 0 else None,
            )
        axes[3, column].axhline(
            500, color="#bb4646", linestyle="--", label="500 ms SLO"
        )
        for row in range(4):
            axis = axes[row, column]
            axis.axvspan(recovery, duration, color="#e8f2ee", alpha=0.7, zorder=-2)
            axis.axvspan(duration, end_seconds, color="#f0f1f3", alpha=0.7, zorder=-2)
            axis.axvline(duration, color="#adb3bb", linewidth=0.8)
            axis.grid(axis="y", color="#dfe3e8", linewidth=0.6)
            axis.spines[["top", "right"]].set_visible(False)
            axis.set_xlim(-1, end_seconds + 2)
            axis.set_ylim(bottom=0)
            if row:
                axis.legend(loc="upper left", fontsize=8, framealpha=0.9)
        axes[3, column].set_xlabel("Seconds from this treatment's load start")
    for row, label in enumerate(
        (
            "Offered events / s",
            "Running workers",
            "Events / messages",
            "Ingestion p95 (ms)",
        )
    ):
        axes[row, 0].set_ylabel(label)
    axes[1, 0].set_ylim(
        0,
        max(9, report.elastic.maximum_workers or 1, report.fixed.maximum_workers or 1)
        + 0.5,
    )
    axes[3, 0].set_ylim(
        0,
        max(
            550,
            *(
                w["p95_ms"]
                for item in summaries
                for w in timing_windows(
                    item.measurement.request_timings, item.measurement.load_started_at
                )
            ),
            *(
                1000 * p.value
                for item in summaries
                for p in item.cloudwatch.series_by_id()["alb_p95_latency"].datapoints
            ),
        )
        * 1.05,
    )
    status = (
        "GUARDRAILS PASSED"
        if report.elasticity_demonstrated
        else "CLAIM NOT ESTABLISHED"
    )
    figure.suptitle(
        f"Worker elasticity • {status}\n{report.session_id}",
        fontsize=16,
        fontweight="bold",
    )
    figure.supxlabel(
        "Green: low-rate recovery • Gray: post-load drain • Missing observations are not interpolated",
        fontsize=10,
    )
    try:
        figure.savefig(output / "comparison.png", dpi=160)
        figure.savefig(output / "comparison.svg")
    finally:
        figure.clear()


def render_markdown(report: ElasticityComparisonReport) -> str:
    def value(number, suffix=""):
        return "not established" if number is None else f"{number:g}{suffix}"

    rows = [
        "# Fixed versus elastic workers",
        "",
        report.conclusion,
        "",
        f"Method: `{report.method}`.",
        "",
        "![Aligned fixed/elastic comparison](comparison.png)",
        "",
        "| Measure | Fixed | Elastic |",
        "| --- | ---: | ---: |",
    ]
    for label, field, suffix in (
        (
            "Highest supported short-step rate",
            "highest_supported_rate_per_second",
            " events/s",
        ),
        ("Maximum observed running workers", "maximum_workers", ""),
        ("First scale-out, after load start", "scale_out_seconds_after_start", " s"),
        (
            "Return to one, after load start",
            "return_to_minimum_seconds_after_start",
            " s",
        ),
        (
            "Stable drain begins, after load ends",
            "stable_drain_seconds_after_load",
            " s",
        ),
        (
            "180s drain confirmed, after load ends",
            "drain_confirmed_seconds_after_load",
            " s",
        ),
        ("Maximum sampled SQS work", "maximum_sampled_queue_work", ""),
        (
            "Maximum native oldest-message age",
            "maximum_native_oldest_age_seconds",
            " s",
        ),
        ("Maximum driver step p95", "maximum_driver_step_p95_ms", " ms"),
    ):
        rows.append(
            f"| {label} | {value(getattr(report.fixed, field), suffix)} | {value(getattr(report.elastic, field), suffix)} |"
        )
    rows.extend(
        [
            "",
            "## Per-step completion support",
            "",
            "| Treatment / step | Offered events/s | Observed window (s) | Completed events/s | Backlog change | Result |",
            "| --- | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for treatment in (report.fixed, report.elastic):
        for step in treatment.step_results:
            rows.append(
                f"| {treatment.treatment} / {step.step_name} | {step.offered_rate_per_second} | {value(step.window_started_seconds)}–{value(step.window_ended_seconds)} | {value(step.completed_events_per_second)} | {value(step.outstanding_change)} | {'pass' if step.supported else ', '.join(step.rejection_reasons)} |"
            )
    rows.extend(
        [
            "",
            "## Interpretation and limits",
            "",
            (
                "These are short, ordered waveform plateaus—not independent steady-state capacity tests. "
                "The reported multiplier compares observed supported steps, not maximum production capacity. "
                "A scale-out result and a rate improvement are separate claims."
            ),
            "",
            (
                "A step requires the shared ingestion, correctness, non-worker headroom and final drain gates; "
                "complete observations; completion throughput at least the offered rate over its actual sampled window; "
                "non-growing outstanding backlog; at most 1,500 outstanding events/messages; and native oldest-message "
                "age at most 180 seconds. Samples must bracket the plateau within 30 seconds and cover at least "
                "max(30 seconds, plateau duration minus 60 seconds). Every occurrence of a rate must pass "
                "before it contributes to the highest supported rate. No interpolation or missing-data zero fill "
                "is performed by this report."
            ),
            "",
            (
                "Completion counts are cumulative and may include earlier arrivals. Throughput and non-growing "
                "backlog together demonstrate observed processing support, not per-event delivery latency. "
                "Scaling and drain times are first observed samples, not exact transition timestamps. "
                "Native p95 values retain their true 60-second buckets; driver p95 is reported per step, never averaged."
                " Demo v5/v6 gate each phase on complete individual ingestion samples, p95 <500 ms and errors <1%; "
                "native ALB p95 remains corroborating evidence, not the ingestion gate. Ten-second latency windows "
                "are displays with variable sample counts, not independent SLO gates. For v5/v6, the minimum sampled "
                "completion window is max(10 seconds, plateau duration minus 60 seconds). Historical profiles retain their original gates. V6 recovery requires a 60-second live ECS service-count suffix with native corroboration inside that suffix; it does not claim that all retiring task containers have stopped or ceased billing."
            ),
            "",
            (
                f"Fixed qualification failures: {', '.join(report.fixed.rejection_reasons) or 'none'}. "
                f"Additional measurement gaps: {', '.join(report.fixed.measurement_rejection_reasons) or 'none'}."
            ),
            "",
            (
                f"Elastic qualification failures: {', '.join(report.elastic.rejection_reasons) or 'none'}. "
                f"Additional measurement gaps: {', '.join(report.elastic.measurement_rejection_reasons) or 'none'}."
            ),
            "",
            "## Provenance",
            "",
            (
                f"Session: `{report.session_id}`. Region: `{report.region}`. Application revision: `{report.git_revision}`. "
                f"Teardown verified: {report.teardown_verified_at.isoformat()}. "
                "Source-file SHA-256 hashes are in `comparison-report.json`. This generator makes no cloud calls "
                "and does not modify the session journal or source evidence."
            ),
            "",
        ]
    )
    return "\n".join(rows)


def generate_elasticity_report(
    session_directory: Path,
    *,
    output_directory: Path | None = None,
    plotter: Callable[..., None] = render_comparison_figure,
) -> ElasticityComparisonReport:
    """Stage all output before publishing; refuse existing report directories."""
    output = output_directory or session_directory / "elasticity/report"
    if output.exists() or output.is_symlink():
        raise ElasticityReportError(
            "report output already exists; choose a new directory"
        )
    fixed, elastic, manifest, verified, hashes = load_comparison_evidence(
        session_directory
    )
    report = build_comparison(
        fixed,
        elastic,
        session_id=manifest["session_id"],
        region=manifest["region"],
        git_revision=manifest["git_revision"],
        teardown_verified_at=verified,
        source_sha256=hashes,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=".elasticity-report-", dir=output.parent
    ) as temporary:
        staging = Path(temporary) / "report"
        staging.mkdir()
        (staging / "comparison-report.json").write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (staging / "comparison-report.md").write_text(
            render_markdown(report), encoding="utf-8"
        )
        try:
            plotter(report, fixed, elastic, staging)
            if any(
                not (staging / filename).is_file()
                or (staging / filename).stat().st_size == 0
                for filename in ("comparison.png", "comparison.svg")
            ):
                raise ElasticityReportError(
                    "plot generator did not produce both nonempty figures"
                )
            if output.exists() or output.is_symlink():
                raise ElasticityReportError("report output appeared during generation")
            staging.rename(output)
        except (OSError, RuntimeError, ValueError) as error:
            raise ElasticityReportError(
                "report rendering failed; no completed report was published"
            ) from error
    return report


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--session-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = generate_elasticity_report(
            arguments.session_directory, output_directory=arguments.output_directory
        )
    except (ElasticityReportError, OSError, ValueError) as error:
        raise SystemExit(f"AWS elasticity report failed: {error}") from error
    print(report.conclusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
