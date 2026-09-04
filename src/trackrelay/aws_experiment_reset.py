"""Reset and prove empty Stage 9.6 state without recreating infrastructure."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from json import JSONDecodeError, dumps, loads
from pathlib import Path
from signal import SIGTERM, getsignal, signal
from time import sleep
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_async_deployment import (
    CLUSTER_NAME_PATTERN,
    FIXED_SERVICE_CAPACITY,
    SERVICE_NAME_PATTERN,
    AwsAsyncDeploymentError,
    ProcessRunner,
    aws_prefix,
    invoke,
    require_clean_approved_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_fixed_control import FixedControlSummary
from trackrelay.aws_observability_inputs import valid_async_observability_dimensions
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
from trackrelay.downstream.control import SimulatorMode
from trackrelay.operator_status import (
    PeriodicStatus,
    operator_failure,
    operator_status,
    status_activity,
)
from trackrelay.services.experiment_reset import (
    ExperimentResetEvidence,
    ExperimentStateSnapshot,
)

NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
Now = Callable[[], datetime]
Sleeper = Callable[[float], None]
SessionAction = Callable[..., object]
ResetRunner = Callable[..., "ExperimentResetResult"]


class AwsExperimentResetError(RuntimeError):
    """A safe, actionable failure in the between-treatment reset."""


class AwsExperimentResetCleanupError(RuntimeError):
    """Report reset cleanup failures without hiding the original failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException,
        cleanup_errors: Sequence[BaseException],
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


class ResetQueueState(BaseModel):
    """Approximate SQS work remaining at one verification point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    visible_messages: NonNegativeInteger
    in_flight_messages: NonNegativeInteger
    delayed_messages: NonNegativeInteger

    @property
    def empty(self) -> bool:
        return not (
            self.visible_messages or self.in_flight_messages or self.delayed_messages
        )


class ExperimentResetObservation(BaseModel):
    """One aligned application, queue, and fixed-worker verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    seconds_after_purge: NonNegativeFloat
    application: ExperimentStateSnapshot
    source_queue: ResetQueueState
    dead_letter_queue: ResetQueueState
    worker_desired_count: NonNegativeInteger
    worker_running_count: NonNegativeInteger
    worker_pending_count: NonNegativeInteger

    @property
    def empty_and_fixed(self) -> bool:
        return (
            self.application.empty_and_healthy
            and self.source_queue.empty
            and self.dead_letter_queue.empty
            and self.worker_desired_count == 1
            and self.worker_running_count == 1
            and self.worker_pending_count == 0
        )


class ExperimentResetResult(BaseModel):
    """Proof that treatment state is empty while infrastructure is unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    fixed_test_run_id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime
    minimum_purge_wait_seconds: Literal[60] = 60
    empty_stability_seconds: Literal[30] = 30
    application_reset: ExperimentResetEvidence
    observations: tuple[ExperimentResetObservation, ...]
    autoscaling_target_absent_before: bool
    autoscaling_target_absent_after: bool

    @model_validator(mode="after")
    def require_verified_isolation(self) -> "ExperimentResetResult":
        if self.completed_at <= self.started_at:
            raise ValueError("experiment reset window must be positive")
        if self.application_reset.test_run_id != self.fixed_test_run_id:
            raise ValueError("experiment reset used another fixed run")
        if not self.observations:
            raise ValueError("experiment reset has no post-purge observations")
        observation_times = tuple(item.observed_at for item in self.observations)
        if observation_times != tuple(sorted(observation_times)):
            raise ValueError("experiment reset observations are not ordered")
        if any(
            item.seconds_after_purge < self.minimum_purge_wait_seconds
            or item.observed_at < self.started_at
            or item.observed_at > self.completed_at
            for item in self.observations
        ):
            raise ValueError("experiment reset observations fall outside its window")
        stable_suffix: list[ExperimentResetObservation] = []
        for item in reversed(self.observations):
            if not item.empty_and_fixed:
                break
            stable_suffix.append(item)
        if not stable_suffix:
            raise ValueError("experiment reset did not finish empty and fixed")
        stable_suffix.reverse()
        if (
            stable_suffix[-1].observed_at - stable_suffix[0].observed_at
        ).total_seconds() < self.empty_stability_seconds:
            raise ValueError("experiment reset lacks a stable empty window")
        if not (
            self.autoscaling_target_absent_before
            and self.autoscaling_target_absent_after
        ):
            raise ValueError("worker autoscaling changed during reset")
        return self


def validate_experiment_reset_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> tuple[dict[str, object], FixedControlSummary]:
    """Require the exact qualified fixed control and unchanged approval."""
    if not session.session_id.startswith("cloud-session-4-"):
        raise AwsSessionError("Stage 9.6 reset must use cloud session 4")
    if session.deployment_mode != "async":
        raise AwsSessionError("Stage 9.6 reset requires async mode")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "fixed_control_qualified":
        raise AwsSessionError("reset requires a qualified fixed control")
    recorded_ceiling = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded_ceiling, str) or parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    ) != parse_positive_money(recorded_ceiling, field_name="recorded cost ceiling"):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    fixed = manifest.get("fixed_control")
    if not isinstance(fixed, dict) or fixed.get("summary") != (
        "elasticity/fixed/summary.json"
    ):
        raise AwsSessionError("qualified fixed-control summary is not recorded")
    try:
        summary = FixedControlSummary.model_validate_json(
            (session.evidence_dir / fixed["summary"]).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise AwsSessionError("qualified fixed-control summary is invalid") from error
    if not summary.qualification.qualified:
        raise AwsSessionError("fixed-control summary is not qualified")
    return manifest, summary


def _valid_api_url(value: str) -> bool:
    try:
        endpoint = urlsplit(value)
        port = endpoint.port
    except ValueError:
        return False
    return (
        endpoint.scheme == "http"
        and bool(endpoint.hostname)
        and endpoint.username is None
        and endpoint.password is None
        and port is None
        and endpoint.path in ("", "/")
        and not endpoint.query
        and not endpoint.fragment
    )


def _valid_queue_url(value: str, *, region: str) -> bool:
    try:
        endpoint = urlsplit(value)
        port = endpoint.port
    except ValueError:
        return False
    parts = tuple(part for part in endpoint.path.split("/") if part)
    return (
        endpoint.scheme == "https"
        and endpoint.hostname == f"sqs.{region}.amazonaws.com"
        and endpoint.username is None
        and endpoint.password is None
        and port is None
        and len(parts) == 2
        and not endpoint.query
        and not endpoint.fragment
    )


def _queue_state(
    session: AwsSession,
    queue_url: str,
    *,
    runner: ProcessRunner,
) -> ResetQueueState:
    result = invoke(
        runner,
        (
            *aws_prefix(session),
            "sqs",
            "get-queue-attributes",
            "--queue-url",
            queue_url,
            "--attribute-names",
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
            "--query",
            "Attributes",
            "--output",
            "json",
        ),
        action="SQS reset verification",
    )
    try:
        attributes = loads(result.stdout)
        return ResetQueueState(
            visible_messages=int(attributes["ApproximateNumberOfMessages"]),
            in_flight_messages=int(attributes["ApproximateNumberOfMessagesNotVisible"]),
            delayed_messages=int(attributes["ApproximateNumberOfMessagesDelayed"]),
        )
    except (JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AwsExperimentResetError("SQS returned invalid reset state") from error


def _worker_counts(
    session: AwsSession,
    *,
    cluster_name: str,
    worker_service_name: str,
    runner: ProcessRunner,
) -> tuple[int, int, int]:
    result = invoke(
        runner,
        (
            *aws_prefix(session),
            "ecs",
            "describe-services",
            "--cluster",
            cluster_name,
            "--services",
            worker_service_name,
            "--query",
            "services[0].[desiredCount,runningCount,pendingCount]",
            "--output",
            "json",
        ),
        action="ECS reset verification",
    )
    try:
        values = tuple(int(value) for value in loads(result.stdout))
    except (JSONDecodeError, TypeError, ValueError) as error:
        raise AwsExperimentResetError("ECS returned invalid reset state") from error
    if len(values) != 3 or min(values) < 0:
        raise AwsExperimentResetError("ECS returned invalid reset state")
    return values


def _autoscaling_target_absent(
    session: AwsSession,
    *,
    cluster_name: str,
    worker_service_name: str,
    runner: ProcessRunner,
) -> bool:
    result = invoke(
        runner,
        (
            *aws_prefix(session),
            "application-autoscaling",
            "describe-scalable-targets",
            "--service-namespace",
            "ecs",
            "--resource-ids",
            f"service/{cluster_name}/{worker_service_name}",
            "--scalable-dimension",
            "ecs:service:DesiredCount",
            "--query",
            "ScalableTargets",
            "--output",
            "json",
        ),
        action="worker autoscaling absence verification",
    )
    try:
        targets = loads(result.stdout)
    except (JSONDecodeError, TypeError) as error:
        raise AwsExperimentResetError(
            "AWS returned invalid autoscaling state"
        ) from error
    if not isinstance(targets, list):
        raise AwsExperimentResetError("AWS returned invalid autoscaling state")
    return targets == []


def _api_request(
    client: httpx.Client,
    method: Literal["GET", "POST"],
    path: Literal["/api/v1/experiments/state", "/api/v1/experiments/reset"],
    *,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    """Expose a fixed operation label/status, never arbitrary HTTP error text.

    These errors are retained by the session journal and printed before cleanup.
    Do not retry here: the reset POST can have partially committed state.
    """
    operation = f"Experiment reset {method} {path}"
    try:
        response = client.request(method, path, json=json)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise AwsExperimentResetError(
            f"{operation} failed: HTTP {error.response.status_code}"
        ) from error
    except httpx.RequestError as error:
        raise AwsExperimentResetError(
            f"{operation} failed: {type(error).__name__}"
        ) from error
    return response


def _application_state(client: httpx.Client) -> ExperimentStateSnapshot:
    response = _api_request(client, "GET", "/api/v1/experiments/state")
    try:
        return ExperimentStateSnapshot.model_validate(response.json())
    except ValueError as error:
        raise AwsExperimentResetError(
            "API returned invalid experiment state"
        ) from error


def _observe(
    session: AwsSession,
    *,
    client: httpx.Client,
    source_queue_url: str,
    dead_letter_queue_url: str,
    cluster_name: str,
    worker_service_name: str,
    purge_started_at: datetime,
    observed_at: datetime,
    runner: ProcessRunner,
) -> ExperimentResetObservation:
    desired, running, pending = _worker_counts(
        session,
        cluster_name=cluster_name,
        worker_service_name=worker_service_name,
        runner=runner,
    )
    return ExperimentResetObservation(
        observed_at=observed_at,
        seconds_after_purge=max(
            0,
            (observed_at - purge_started_at).total_seconds(),
        ),
        application=_application_state(client),
        source_queue=_queue_state(session, source_queue_url, runner=runner),
        dead_letter_queue=_queue_state(
            session,
            dead_letter_queue_url,
            runner=runner,
        ),
        worker_desired_count=desired,
        worker_running_count=running,
        worker_pending_count=pending,
    )


def _write_observations(
    evidence_root: Path,
    observations: Sequence[ExperimentResetObservation],
) -> None:
    (evidence_root / "observations.json").write_text(
        dumps([item.model_dump(mode="json") for item in observations], indent=2) + "\n",
        encoding="utf-8",
    )


def execute_experiment_reset(
    session: AwsSession,
    *,
    fixed_summary: FixedControlSummary,
    api_url: str,
    source_queue_url: str,
    dead_letter_queue_url: str,
    cluster_name: str,
    worker_service_name: str,
    evidence_root: Path,
    runner: ProcessRunner = run_process,
    now: Now = lambda: datetime.now(UTC),
    sleeper: Sleeper = sleep,
    poll_interval_seconds: float = 10,
    verification_timeout_seconds: float = 120,
    client: httpx.Client | None = None,
) -> ExperimentResetResult:
    """Reset application state, purge queues, and prove stable isolation."""
    client_context = (
        client if client is not None else httpx.Client(base_url=api_url, timeout=30)
    )
    close_client = client is None
    started_at = now()
    try:
        before = _observe(
            session,
            client=client_context,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            purge_started_at=started_at,
            observed_at=started_at,
            runner=runner,
        )
        (evidence_root / "pre-reset-observation.json").write_text(
            before.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        expected_count = fixed_summary.measurement.definition.expected_request_count
        if (
            not before.source_queue.empty
            or not before.dead_letter_queue.empty
            or before.worker_desired_count != 1
            or before.worker_running_count != 1
            or before.worker_pending_count != 0
            or before.application.database.test_runs != 1
            or before.application.database.events != expected_count
            or before.application.database.delivery_outbox_entries != expected_count
            or before.application.database.shipments != expected_count
            or before.application.simulator_receipts != expected_count
            or before.application.simulator_mode is not SimulatorMode.HEALTHY
        ):
            raise AwsExperimentResetError(
                "pre-reset state differs from the qualified fixed control"
            )
        absent_before = _autoscaling_target_absent(
            session,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            runner=runner,
        )
        if not absent_before:
            raise AwsExperimentResetError(
                "worker autoscaling appeared before the treatment reset"
            )
        response = _api_request(
            client_context,
            "POST",
            "/api/v1/experiments/reset",
            json={
                "test_run_id": str(fixed_summary.measurement.test_run_id),
                "expected_event_count": expected_count,
            },
        )
        try:
            application_reset = ExperimentResetEvidence.model_validate(response.json())
        except ValueError as error:
            raise AwsExperimentResetError(
                "API returned invalid reset evidence"
            ) from error
        (evidence_root / "application-reset.json").write_text(
            application_reset.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        purge_started_at = now()
        operator_status("Reset: purging source queue and dead-letter queue")
        for queue_url in (source_queue_url, dead_letter_queue_url):
            invoke(
                runner,
                (*aws_prefix(session), "sqs", "purge-queue", "--queue-url", queue_url),
                action="SQS treatment reset purge",
            )
        with status_activity("Reset: waiting 60s for SQS purge propagation"):
            sleeper(60)

        observations: list[ExperimentResetObservation] = []
        stable_since: datetime | None = None
        reset_progress = PeriodicStatus()
        while True:
            observed_at = now()
            observation = _observe(
                session,
                client=client_context,
                source_queue_url=source_queue_url,
                dead_letter_queue_url=dead_letter_queue_url,
                cluster_name=cluster_name,
                worker_service_name=worker_service_name,
                purge_started_at=purge_started_at,
                observed_at=observed_at,
                runner=runner,
            )
            observations.append(observation)
            _write_observations(evidence_root, observations)
            if not observation.empty_and_fixed:
                stable_since = None
            elif stable_since is None:
                stable_since = observed_at
            elif (observed_at - stable_since).total_seconds() >= 30:
                operator_status("Reset: stable empty application/queue state confirmed")
                break
            elapsed = (observed_at - purge_started_at).total_seconds()
            stable_seconds = (
                (observed_at - stable_since).total_seconds() if stable_since else 0
            )
            reset_progress.update(
                elapsed,
                f"Reset verification: {elapsed:.0f}/{verification_timeout_seconds:.0f}s; "
                f"empty_and_fixed={observation.empty_and_fixed}; stable={stable_seconds:.0f}/30s",
            )
            if (observed_at - purge_started_at).total_seconds() >= (
                verification_timeout_seconds
            ):
                raise AwsExperimentResetError(
                    "experiment state did not remain stably empty after purge"
                )
            sleeper(poll_interval_seconds)
        absent_after = _autoscaling_target_absent(
            session,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            runner=runner,
        )
        if not absent_after:
            raise AwsExperimentResetError(
                "worker autoscaling appeared during the treatment reset"
            )
        result = ExperimentResetResult(
            fixed_test_run_id=fixed_summary.measurement.test_run_id,
            started_at=started_at,
            completed_at=now(),
            application_reset=application_reset,
            observations=tuple(observations),
            autoscaling_target_absent_before=absent_before,
            autoscaling_target_absent_after=absent_after,
        )
        (evidence_root / "result.json").write_text(
            result.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        if close_client:
            client_context.close()


def _prepare_reset(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    runner: ProcessRunner,
) -> tuple[str, str, str, str, str, Path]:
    _current, revision = require_clean_approved_revision(session, runner=runner)
    api_url = terraform_output(session, "async_api_url", runner=runner)
    source_queue_url = terraform_output(session, "delivery_queue_url", runner=runner)
    dead_letter_queue_url = terraform_output(
        session,
        "delivery_dead_letter_queue_url",
        runner=runner,
    )
    dimensions = terraform_output(
        session,
        "async_observability_dimensions",
        runner=runner,
        json_output=True,
    )
    capacity = terraform_output(
        session,
        "async_service_capacity",
        runner=runner,
        json_output=True,
    )
    if (
        not isinstance(api_url, str)
        or not _valid_api_url(api_url)
        or not isinstance(source_queue_url, str)
        or not _valid_queue_url(source_queue_url, region=session.region)
        or not isinstance(dead_letter_queue_url, str)
        or not _valid_queue_url(dead_letter_queue_url, region=session.region)
        or not valid_async_observability_dimensions(dimensions)
        or not isinstance(dimensions.get("cluster_name"), str)
        or CLUSTER_NAME_PATTERN.fullmatch(dimensions["cluster_name"]) is None
        or not isinstance(dimensions.get("worker_service_name"), str)
        or SERVICE_NAME_PATTERN.fullmatch(dimensions["worker_service_name"]) is None
        or not isinstance(capacity, dict)
        or set(capacity) != set(FIXED_SERVICE_CAPACITY)
    ):
        raise AwsExperimentResetError("Terraform returned invalid reset inputs")
    for role, expected in FIXED_SERVICE_CAPACITY.items():
        observed = capacity.get(role)
        if not isinstance(observed, dict):
            raise AwsExperimentResetError("fixed capacity changed before reset")
        numeric = {
            key: observed.get(key)
            for key in ("cpu_units", "desired_count", "memory_mib")
        }
        service_name = observed.get("service_name")
        if (
            set(observed)
            != {"cpu_units", "desired_count", "memory_mib", "service_name"}
            or any(type(value) is not int for value in numeric.values())
            or numeric != expected
            or not isinstance(service_name, str)
            or SERVICE_NAME_PATTERN.fullmatch(service_name) is None
            or not service_name.endswith(f"-{role}")
        ):
            raise AwsExperimentResetError("fixed capacity changed before reset")
    if (
        capacity["api"]["service_name"] != dimensions["api_service_name"]
        or capacity["simulator"]["service_name"] != dimensions["simulator_service_name"]
        or capacity["worker"]["service_name"] != dimensions["worker_service_name"]
    ):
        raise AwsExperimentResetError("Terraform returned inconsistent reset inputs")
    evidence_root = session.evidence_dir / "elasticity" / "reset"
    evidence_root.mkdir(parents=True, exist_ok=False)
    manifest["experiment_reset"] = {
        "git_revision": revision,
        "unconditional_teardown_armed": True,
    }
    manifest["status"] = "experiment_reset_armed"
    write_manifest(session, manifest)
    return (
        api_url,
        source_queue_url,
        dead_letter_queue_url,
        dimensions["cluster_name"],
        dimensions["worker_service_name"],
        evidence_root,
    )


def run_experiment_reset_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    reset_runner: ResetRunner = execute_experiment_reset,
    runner: ProcessRunner = run_process,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
) -> ExperimentResetResult:
    """Prove reset isolation or unconditionally destroy and verify the session."""
    manifest, fixed_summary = validate_experiment_reset_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    try:
        (
            api_url,
            source_queue_url,
            dead_letter_queue_url,
            cluster_name,
            worker_service_name,
            evidence_root,
        ) = _prepare_reset(session, manifest=manifest, runner=runner)
        result = reset_runner(
            session,
            fixed_summary=fixed_summary,
            api_url=api_url,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            evidence_root=evidence_root,
            runner=runner,
        )
        manifest = load_manifest(session)
        manifest["experiment_reset"] = {
            **manifest["experiment_reset"],
            "fixed_test_run_id": str(result.fixed_test_run_id),
            "result": "elasticity/reset/result.json",
        }
        manifest["status"] = "experiment_reset_verified"
        write_manifest(session, manifest)
        return result
    except BaseException as workflow_error:
        operator_failure("Phase reset; beginning cleanup", workflow_error)
        cleanup_errors = []
        try:
            destroyer(session)
        except BaseException as error:  # noqa: BLE001 - still verify natively
            cleanup_errors.append(error)
        try:
            teardown_verifier(session)
        except BaseException as error:  # noqa: BLE001 - report every cleanup failure
            cleanup_errors.append(error)
        if cleanup_errors:
            raise AwsExperimentResetCleanupError(
                "Stage 9.6 reset failed and cleanup did not complete",
                workflow_error=workflow_error,
                cleanup_errors=cleanup_errors,
            ) from workflow_error
        raise


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument(
        "--approved-unconditional-teardown-session-id",
        required=True,
    )
    return parser


def run_from_arguments(arguments: Namespace) -> None:
    run_experiment_reset_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during Stage 9.6 reset")


def main(argv: Sequence[str] | None = None) -> int:
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsAsyncDeploymentError,
        AwsExperimentResetCleanupError,
        AwsExperimentResetError,
        AwsSessionError,
        KeyboardInterrupt,
        httpx.HTTPError,
    ) as error:
        raise SystemExit(f"AWS experiment reset failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
