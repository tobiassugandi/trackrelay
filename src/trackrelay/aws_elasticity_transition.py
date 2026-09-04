"""Enable and prove the sole Stage 9.7 worker-autoscaling intervention."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from json import JSONDecodeError, loads
from signal import SIGTERM, getsignal, signal
from time import monotonic, sleep
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_async_deployment import (
    CLUSTER_NAME_PATTERN,
    FIXED_SERVICE_CAPACITY,
    IMAGE_DIGEST_PATTERN,
    IMAGE_ROLES,
    SERVICE_NAME_PATTERN,
    AwsAsyncDeploymentError,
    ProcessRunner,
    aws_prefix,
    invoke,
    require_clean_approved_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_experiment_reset import (
    ExperimentResetResult,
    ResetQueueState,
)
from trackrelay.aws_observability_inputs import valid_async_observability_dimensions
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    destroy_session,
    file_sha256,
    load_manifest,
    parse_positive_money,
    session_from_arguments,
    verify_destroyed,
    write_command_log,
    write_manifest,
)
from trackrelay.operator_status import operator_failure
from trackrelay.services.experiment_reset import ExperimentStateSnapshot

NonNegativeInteger = Annotated[int, Field(ge=0)]
PositiveInteger = Annotated[int, Field(gt=0)]
Timer = Callable[[], float]
Sleeper = Callable[[float], None]
SessionAction = Callable[..., object]
TransitionRunner = Callable[..., "ElasticityTransitionEvidence"]

MEANINGFUL_RESOURCE_ADDRESSES = (
    "aws_appautoscaling_policy.async_worker_scale_in[0]",
    "aws_appautoscaling_policy.async_worker_scale_out[0]",
    "aws_appautoscaling_target.async_worker[0]",
    "aws_cloudwatch_metric_alarm.async_worker_backlog_high[0]",
    "aws_cloudwatch_metric_alarm.async_worker_empty[0]",
)


class AwsElasticityTransitionError(RuntimeError):
    """A safe, actionable failure in the worker-policy transition."""


class AwsElasticityTransitionCleanupError(RuntimeError):
    """Report transition cleanup failures without hiding the original failure."""

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


class WorkerAutoscalingPolicy(BaseModel):
    """Frozen, bounded treatment policy exposed by Terraform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal[2, 3, 4] = 2
    backlog_threshold_messages: Literal[10] | None = 10
    empty_alarm_name: str
    high_alarm_name: str
    maximum_capacity: Literal[8] = 8
    metric_period_seconds: Literal[60] = 60
    minimum_capacity: Literal[1] = 1
    queue_name: str
    resource_id: str
    scale_in_cooldown_seconds: Literal[60] = 60
    scale_in_evaluation_periods: Literal[3] = 3
    scale_in_messages_per_minute: Literal[120] | None = None
    scale_in_queue_work_threshold: Literal[10] | None = None
    scale_in_policy_name: str
    scale_out_cooldown_seconds: Literal[60] = 60
    scale_out_evaluation_periods: Literal[1, 2] = 1
    scale_out_messages_per_minute: Literal[300] | None = None
    scale_out_period_seconds: Literal[10] | None = None
    scale_out_rate_per_second: Literal[3] | None = None
    scale_out_policy_name: str

    @model_validator(mode="after")
    def require_coherent_names(self) -> "WorkerAutoscalingPolicy":
        resource_parts = self.resource_id.split("/")
        if (
            len(resource_parts) != 3
            or resource_parts[0] != "service"
            or not resource_parts[1].endswith("-async")
            or not resource_parts[2].endswith("-worker")
            or not self.queue_name.endswith("-delivery")
            or resource_parts[1].removesuffix("-async")
            != resource_parts[2].removesuffix("-worker")
            or resource_parts[2].removesuffix("-worker")
            != self.queue_name.removesuffix("-delivery")
        ):
            raise ValueError("autoscaling policy uses an invalid queue")
        expected_prefix = resource_parts[2]
        expected_high_suffix = (
            "demand-high" if self.policy_version >= 3 else "backlog-high"
        )
        expected_low_suffix = "release-safe" if self.policy_version >= 3 else "empty"
        if (
            self.high_alarm_name != f"{expected_prefix}-{expected_high_suffix}"
            or self.empty_alarm_name != f"{expected_prefix}-{expected_low_suffix}"
            or self.scale_out_policy_name != f"{expected_prefix}-scale-out"
            or self.scale_in_policy_name != f"{expected_prefix}-scale-in"
        ):
            raise ValueError("autoscaling resource names are inconsistent")
        if self.policy_version == 2 and (
            self.backlog_threshold_messages != 10
            or self.scale_out_messages_per_minute is not None
            or self.scale_in_messages_per_minute is not None
            or self.scale_in_queue_work_threshold is not None
        ):
            raise ValueError("candidate v2 autoscaling thresholds are inconsistent")
        if self.policy_version == 3 and (
            self.backlog_threshold_messages is not None
            or self.scale_out_messages_per_minute != 300
            or self.scale_in_messages_per_minute != 120
            or self.scale_in_queue_work_threshold != 10
        ):
            raise ValueError("candidate v3 autoscaling thresholds are inconsistent")
        if self.policy_version < 4 and (
            self.scale_out_period_seconds is not None
            or self.scale_out_rate_per_second is not None
            or self.scale_out_evaluation_periods != 1
        ):
            raise ValueError("historical policies cannot use high-resolution demand")
        if self.policy_version == 4 and (
            self.backlog_threshold_messages is not None
            or self.scale_out_messages_per_minute is not None
            or self.scale_out_period_seconds != 10
            or self.scale_out_rate_per_second != 3
            or self.scale_out_evaluation_periods != 2
            or self.scale_in_messages_per_minute != 120
            or self.scale_in_queue_work_threshold != 10
        ):
            raise ValueError("policy v4 high-resolution thresholds are inconsistent")
        return self


class ElasticityTransitionPlanEvidence(BaseModel):
    """Proof that Terraform changes only the five policy resources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_addresses: tuple[str, ...]
    resource_actions: tuple[Literal["create"], ...]
    output_action: Literal["create"]

    @model_validator(mode="after")
    def require_exact_intervention(self) -> "ElasticityTransitionPlanEvidence":
        if self.resource_addresses != MEANINGFUL_RESOURCE_ADDRESSES:
            raise ValueError("transition plan changes unexpected resources")
        if len(self.resource_actions) != len(self.resource_addresses):
            raise ValueError("transition plan actions are incomplete")
        return self


class WorkerScalingPolicyVerification(BaseModel):
    """Sanitized native details for one scaling direction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    cooldown_seconds: Literal[60]
    scaling_adjustment: PositiveInteger
    metric_interval_lower_bound: float | None = None
    metric_interval_upper_bound: float | None = None


class WorkerAlarmVerification(BaseModel):
    """Sanitized native alarm fields that govern one policy direction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    actions_enabled: bool
    comparison_operator: Literal[
        "GreaterThanOrEqualToThreshold",
        "LessThanThreshold",
    ]
    datapoints_to_alarm: PositiveInteger
    evaluation_periods: PositiveInteger
    threshold: NonNegativeInteger
    treat_missing_data: Literal["breaching", "notBreaching"]
    signal: Literal[
        "visible_backlog", "messages_sent", "arrival_rate", "low_demand_and_queue_work"
    ] = "visible_backlog"
    metric_query_ids: tuple[str, ...] = ()


class WorkerAutoscalingVerification(BaseModel):
    """Native AWS proof that the frozen target, policies, and alarms exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    collected_at: AwareDatetime
    resource_id: str
    minimum_capacity: Literal[1]
    maximum_capacity: Literal[8]
    suspended: bool
    policies: tuple[WorkerScalingPolicyVerification, ...]
    alarms: tuple[WorkerAlarmVerification, ...]

    def matches(self, expected: WorkerAutoscalingPolicy) -> bool:
        policies = {item.name: item for item in self.policies}
        alarms = {item.name: item for item in self.alarms}
        common_matches = (
            self.resource_id == expected.resource_id
            and self.minimum_capacity == expected.minimum_capacity
            and self.maximum_capacity == expected.maximum_capacity
            and not self.suspended
            and len(self.policies) == 2
            and len(self.alarms) == 2
            and set(policies)
            == {expected.scale_in_policy_name, expected.scale_out_policy_name}
            and policies[expected.scale_in_policy_name].cooldown_seconds
            == expected.scale_in_cooldown_seconds
            and policies[expected.scale_in_policy_name].scaling_adjustment
            == expected.minimum_capacity
            and policies[expected.scale_out_policy_name].cooldown_seconds
            == expected.scale_out_cooldown_seconds
            and policies[expected.scale_out_policy_name].scaling_adjustment
            == expected.maximum_capacity
            and set(alarms) == {expected.empty_alarm_name, expected.high_alarm_name}
            and alarms[expected.high_alarm_name].comparison_operator
            == "GreaterThanOrEqualToThreshold"
            and alarms[expected.high_alarm_name].actions_enabled
            and alarms[expected.high_alarm_name].evaluation_periods
            == expected.scale_out_evaluation_periods
            and alarms[expected.high_alarm_name].datapoints_to_alarm
            == expected.scale_out_evaluation_periods
            and alarms[expected.high_alarm_name].treat_missing_data == "notBreaching"
            and alarms[expected.empty_alarm_name].actions_enabled
            and alarms[expected.empty_alarm_name].evaluation_periods
            == expected.scale_in_evaluation_periods
            and alarms[expected.empty_alarm_name].datapoints_to_alarm
            == expected.scale_in_evaluation_periods
        )
        if not common_matches:
            return False
        scale_in = policies[expected.scale_in_policy_name]
        scale_out = policies[expected.scale_out_policy_name]
        high = alarms[expected.high_alarm_name]
        low = alarms[expected.empty_alarm_name]
        if expected.policy_version == 2:
            return (
                high.threshold == expected.backlog_threshold_messages
                and high.signal == "visible_backlog"
                and not high.metric_query_ids
                and low.comparison_operator == "LessThanThreshold"
                and low.threshold == 1
                and low.treat_missing_data == "breaching"
                and low.signal == "visible_backlog"
                and not low.metric_query_ids
            )
        return (
            scale_out.metric_interval_lower_bound == 0
            and scale_out.metric_interval_upper_bound is None
            and scale_in.metric_interval_lower_bound == 0
            and scale_in.metric_interval_upper_bound is None
            and high.threshold
            == (
                expected.scale_out_rate_per_second
                if expected.policy_version == 4
                else expected.scale_out_messages_per_minute
            )
            and high.signal
            == ("arrival_rate" if expected.policy_version == 4 else "messages_sent")
            and not high.metric_query_ids
            and low.comparison_operator == "GreaterThanOrEqualToThreshold"
            and low.threshold == 1
            and low.treat_missing_data == "notBreaching"
            and low.signal == "low_demand_and_queue_work"
            and low.metric_query_ids
            == ("delayed", "in_flight", "release_safe", "sent", "visible")
        )


class ElasticityTransitionObservation(BaseModel):
    """Application, queue, and worker state around the policy-only apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    application: ExperimentStateSnapshot
    source_queue: ResetQueueState
    dead_letter_queue: ResetQueueState
    worker_desired_count: NonNegativeInteger
    worker_running_count: NonNegativeInteger
    worker_pending_count: NonNegativeInteger

    @property
    def empty_at_minimum(self) -> bool:
        return (
            self.application.empty_and_healthy
            and self.source_queue.empty
            and self.dead_letter_queue.empty
            and self.worker_desired_count == 1
            and self.worker_running_count == 1
            and self.worker_pending_count == 0
        )


class WorkerPolicyTransitionEvidence(BaseModel):
    """Complete proof of a policy-only change around an empty deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy: WorkerAutoscalingPolicy
    plan: ElasticityTransitionPlanEvidence
    pre_apply: ElasticityTransitionObservation
    post_apply: ElasticityTransitionObservation
    native: WorkerAutoscalingVerification
    applied_at: AwareDatetime
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def require_isolated_verified_change(self) -> "WorkerPolicyTransitionEvidence":
        if not self.pre_apply.empty_at_minimum or not self.post_apply.empty_at_minimum:
            raise ValueError("autoscaling transition changed treatment state")
        if not self.native.matches(self.policy):
            raise ValueError("native autoscaling state differs from the frozen policy")
        if self.verified_at < self.applied_at:
            raise ValueError("autoscaling verification precedes apply")
        return self


class ElasticityTransitionEvidence(WorkerPolicyTransitionEvidence):
    """Paired evidence must retain its real fixed-control reset identity."""

    reset_fixed_test_run_id: UUID


class DiagnosticTransitionEvidence(WorkerPolicyTransitionEvidence):
    """Fresh-deployment diagnostic proof, never a paired reset substitute."""

    kind: Literal["elastic-only-diagnostic"] = "elastic-only-diagnostic"
    reset_fixed_test_run_id: None = None


def _expected_policy(
    *,
    cluster_name: str,
    worker_service_name: str,
    queue_name: str,
) -> WorkerAutoscalingPolicy:
    return WorkerAutoscalingPolicy(
        policy_version=4,
        backlog_threshold_messages=None,
        empty_alarm_name=f"{worker_service_name}-release-safe",
        high_alarm_name=f"{worker_service_name}-demand-high",
        queue_name=queue_name,
        resource_id=f"service/{cluster_name}/{worker_service_name}",
        scale_in_policy_name=f"{worker_service_name}-scale-in",
        scale_in_messages_per_minute=120,
        scale_in_queue_work_threshold=10,
        scale_out_policy_name=f"{worker_service_name}-scale-out",
        scale_out_period_seconds=10,
        scale_out_rate_per_second=3,
        scale_out_evaluation_periods=2,
    )


def _exact_after(resource: dict[str, object], expected: dict[str, object]) -> None:
    try:
        change = resource["change"]
        before = change["before"]
        after = change["after"]
    except (KeyError, TypeError) as error:
        raise AwsElasticityTransitionError(
            "Terraform autoscaling resource change is incomplete"
        ) from error
    if before is not None or not isinstance(after, dict):
        raise AwsElasticityTransitionError(
            "Terraform autoscaling transition is not resource creation"
        )
    if any(after.get(key) != value for key, value in expected.items()):
        raise AwsElasticityTransitionError(
            "Terraform autoscaling resource differs from the frozen policy"
        )


def validate_elasticity_transition_plan(
    plan_json: str,
    *,
    plan_sha256: str,
    expected_policy: WorkerAutoscalingPolicy,
) -> ElasticityTransitionPlanEvidence:
    """Reject any plan that changes more than the five policy resources."""
    try:
        document = loads(plan_json)
        resources = document["resource_changes"]
        output_changes = document["output_changes"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsElasticityTransitionError(
            "Terraform returned an invalid autoscaling plan"
        ) from error
    if not isinstance(resources, list) or not isinstance(output_changes, dict):
        raise AwsElasticityTransitionError(
            "Terraform returned an invalid autoscaling plan"
        )
    meaningful: dict[str, dict[str, object]] = {}
    actions: dict[str, tuple[str, ...]] = {}
    for resource in resources:
        try:
            address = resource["address"]
            resource_actions = tuple(resource["change"]["actions"])
        except (KeyError, TypeError) as error:
            raise AwsElasticityTransitionError(
                "Terraform autoscaling change is invalid"
            ) from error
        if (
            not isinstance(address, str)
            or not isinstance(resource, dict)
            or any(not isinstance(action, str) for action in resource_actions)
        ):
            raise AwsElasticityTransitionError(
                "Terraform autoscaling change is invalid"
            )
        if resource_actions != ("no-op",):
            meaningful[address] = resource
            actions[address] = resource_actions
    if tuple(sorted(meaningful)) != MEANINGFUL_RESOURCE_ADDRESSES or any(
        item != ("create",) for item in actions.values()
    ):
        expected_addresses = set(MEANINGFUL_RESOURCE_ADDRESSES)
        unexpected = tuple(
            f"{address} ({'/'.join(resource_actions)})"
            for address, resource_actions in sorted(actions.items())
            if address not in expected_addresses
        )
        missing = tuple(
            address
            for address in MEANINGFUL_RESOURCE_ADDRESSES
            if address not in actions
        )
        wrong_actions = tuple(
            f"{address} ({'/'.join(actions[address])})"
            for address in MEANINGFUL_RESOURCE_ADDRESSES
            if address in actions and actions[address] != ("create",)
        )
        details = []
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if wrong_actions:
            details.append(f"wrong actions: {', '.join(wrong_actions)}")
        suffix = f"; {'; '.join(details)}" if details else ""
        raise AwsElasticityTransitionError(
            "autoscaling plan must create only the five frozen policy resources"
            f"{suffix}"
        )

    common = {
        "resource_id": expected_policy.resource_id,
        "scalable_dimension": "ecs:service:DesiredCount",
        "service_namespace": "ecs",
    }
    _exact_after(
        meaningful["aws_appautoscaling_target.async_worker[0]"],
        {
            **common,
            "min_capacity": expected_policy.minimum_capacity,
            "max_capacity": expected_policy.maximum_capacity,
        },
    )
    for direction, policy_name, cooldown, capacity in (
        (
            "scale_out",
            expected_policy.scale_out_policy_name,
            expected_policy.scale_out_cooldown_seconds,
            expected_policy.maximum_capacity,
        ),
        (
            "scale_in",
            expected_policy.scale_in_policy_name,
            expected_policy.scale_in_cooldown_seconds,
            expected_policy.minimum_capacity,
        ),
    ):
        resource = meaningful[f"aws_appautoscaling_policy.async_worker_{direction}[0]"]
        _exact_after(
            resource,
            {**common, "name": policy_name, "policy_type": "StepScaling"},
        )
        configuration = resource["change"]["after"].get(
            "step_scaling_policy_configuration"
        )
        step = (
            configuration[0]["step_adjustment"][0]
            if isinstance(configuration, list)
            and len(configuration) == 1
            and isinstance(configuration[0].get("step_adjustment"), list)
            and len(configuration[0]["step_adjustment"]) == 1
            else {}
        )
        interval_matches = (
            step.get("metric_interval_upper_bound") in (0, "0")
            and step.get("metric_interval_lower_bound") is None
            if expected_policy.policy_version == 2 and direction == "scale_in"
            else step.get("metric_interval_lower_bound") in (0, "0")
        )
        if (
            not isinstance(configuration, list)
            or len(configuration) != 1
            or configuration[0].get("adjustment_type") != "ExactCapacity"
            or configuration[0].get("cooldown") != cooldown
            or configuration[0].get("metric_aggregation_type") != "Maximum"
            or step.get("scaling_adjustment") != capacity
            or not interval_matches
        ):
            raise AwsElasticityTransitionError(
                "Terraform scaling steps differ from the frozen policy"
            )

    high_address = "aws_cloudwatch_metric_alarm.async_worker_backlog_high[0]"
    low_address = "aws_cloudwatch_metric_alarm.async_worker_empty[0]"
    if expected_policy.policy_version == 2:
        alarm_expectations = {
            high_address: {
                "actions_enabled": True,
                "alarm_name": expected_policy.high_alarm_name,
                "comparison_operator": "GreaterThanOrEqualToThreshold",
                "datapoints_to_alarm": expected_policy.scale_out_evaluation_periods,
                "evaluation_periods": expected_policy.scale_out_evaluation_periods,
                "threshold": expected_policy.backlog_threshold_messages,
                "treat_missing_data": "notBreaching",
            },
            low_address: {
                "actions_enabled": True,
                "alarm_name": expected_policy.empty_alarm_name,
                "comparison_operator": "LessThanThreshold",
                "datapoints_to_alarm": expected_policy.scale_in_evaluation_periods,
                "evaluation_periods": expected_policy.scale_in_evaluation_periods,
                "threshold": 1,
                "treat_missing_data": "breaching",
            },
        }
        for address, expected in alarm_expectations.items():
            _exact_after(
                meaningful[address],
                {
                    **expected,
                    "dimensions": {"QueueName": expected_policy.queue_name},
                    "metric_name": "ApproximateNumberOfMessagesVisible",
                    "namespace": "AWS/SQS",
                    "period": expected_policy.metric_period_seconds,
                    "statistic": "Maximum",
                },
            )
    else:
        _exact_after(
            meaningful[high_address],
            {
                "actions_enabled": True,
                "alarm_name": expected_policy.high_alarm_name,
                "comparison_operator": "GreaterThanOrEqualToThreshold",
                "datapoints_to_alarm": expected_policy.scale_out_evaluation_periods,
                "dimensions": {"QueueName": expected_policy.queue_name},
                "evaluation_periods": expected_policy.scale_out_evaluation_periods,
                "metric_name": "ArrivalRate"
                if expected_policy.policy_version == 4
                else "NumberOfMessagesSent",
                "namespace": "TrackRelay/Elasticity"
                if expected_policy.policy_version == 4
                else "AWS/SQS",
                "period": expected_policy.scale_out_period_seconds
                if expected_policy.policy_version == 4
                else expected_policy.metric_period_seconds,
                "statistic": "Maximum"
                if expected_policy.policy_version == 4
                else "Sum",
                "threshold": expected_policy.scale_out_rate_per_second
                if expected_policy.policy_version == 4
                else expected_policy.scale_out_messages_per_minute,
                "treat_missing_data": "notBreaching",
            },
        )
        _exact_after(
            meaningful[low_address],
            {
                "actions_enabled": True,
                "alarm_name": expected_policy.empty_alarm_name,
                "comparison_operator": "GreaterThanOrEqualToThreshold",
                "datapoints_to_alarm": expected_policy.scale_in_evaluation_periods,
                "evaluation_periods": expected_policy.scale_in_evaluation_periods,
                "threshold": 1,
                "treat_missing_data": "notBreaching",
            },
        )
        queries = meaningful[low_address]["change"]["after"].get("metric_query")
        if not isinstance(queries, list):
            raise AwsElasticityTransitionError(
                "Terraform scale-in alarm omits its frozen metric queries"
            )
        by_id = {query.get("id"): query for query in queries if isinstance(query, dict)}
        expected_expression = (
            "IF(FILL(sent, 0) < 120, IF(FILL(visible, 0) + "
            "FILL(in_flight, 0) + FILL(delayed, 0) < 10, 1, 0), 0)"
        )
        expected_metrics = {
            "sent": ("NumberOfMessagesSent", "Sum"),
            "visible": ("ApproximateNumberOfMessagesVisible", "Maximum"),
            "in_flight": ("ApproximateNumberOfMessagesNotVisible", "Maximum"),
            "delayed": ("ApproximateNumberOfMessagesDelayed", "Maximum"),
        }
        expression = by_id.get("release_safe", {})
        if (
            set(by_id) != {"release_safe", *expected_metrics}
            or expression.get("expression") != expected_expression
            or expression.get("return_data") is not True
        ):
            raise AwsElasticityTransitionError(
                "Terraform scale-in expression differs from the frozen policy"
            )
        for query_id, (metric_name, statistic) in expected_metrics.items():
            query = by_id[query_id]
            metrics = query.get("metric")
            if (
                query.get("return_data") is not False
                or not isinstance(metrics, list)
                or len(metrics) != 1
                or metrics[0].get("dimensions")
                != {"QueueName": expected_policy.queue_name}
                or metrics[0].get("metric_name") != metric_name
                or metrics[0].get("namespace") != "AWS/SQS"
                or metrics[0].get("period") != expected_policy.metric_period_seconds
                or metrics[0].get("stat") != statistic
            ):
                raise AwsElasticityTransitionError(
                    "Terraform scale-in metrics differ from the frozen policy"
                )

    meaningful_outputs = {
        name: value
        for name, value in output_changes.items()
        if tuple(value.get("actions", ())) != ("no-op",)
    }
    output_change = meaningful_outputs.get("worker_autoscaling_policy")
    if set(meaningful_outputs) != {"worker_autoscaling_policy"} or not isinstance(
        output_change, dict
    ):
        raise AwsElasticityTransitionError(
            "autoscaling plan changes unexpected Terraform outputs"
        )
    if (
        tuple(output_change.get("actions", ())) != ("create",)
        or output_change.get("before") is not None
        or output_change.get("after") != expected_policy.model_dump(mode="json")
    ):
        raise AwsElasticityTransitionError(
            "Terraform autoscaling output differs from the frozen policy"
        )
    try:
        return ElasticityTransitionPlanEvidence(
            plan_sha256=plan_sha256,
            resource_addresses=tuple(sorted(meaningful)),
            resource_actions=tuple(
                actions[address][0] for address in sorted(meaningful)
            ),
            output_action="create",
        )
    except ValueError as error:
        raise AwsElasticityTransitionError(
            "Terraform autoscaling plan proof is invalid"
        ) from error


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
        action="elasticity transition queue verification",
    )
    try:
        attributes = loads(result.stdout)
        return ResetQueueState(
            visible_messages=int(attributes["ApproximateNumberOfMessages"]),
            in_flight_messages=int(attributes["ApproximateNumberOfMessagesNotVisible"]),
            delayed_messages=int(attributes["ApproximateNumberOfMessagesDelayed"]),
        )
    except (JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AwsElasticityTransitionError("SQS returned invalid state") from error


def _transition_observation(
    session: AwsSession,
    *,
    client: httpx.Client,
    source_queue_url: str,
    dead_letter_queue_url: str,
    cluster_name: str,
    worker_service_name: str,
    runner: ProcessRunner,
    observed_at: datetime,
) -> ElasticityTransitionObservation:
    response = client.get("/api/v1/experiments/state")
    response.raise_for_status()
    try:
        application = ExperimentStateSnapshot.model_validate(response.json())
    except ValueError as error:
        raise AwsElasticityTransitionError(
            "API returned invalid experiment state"
        ) from error
    worker = invoke(
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
        action="elasticity transition worker verification",
    )
    try:
        counts = tuple(int(value) for value in loads(worker.stdout))
    except (JSONDecodeError, TypeError, ValueError) as error:
        raise AwsElasticityTransitionError(
            "ECS returned invalid worker state"
        ) from error
    if len(counts) != 3 or min(counts) < 0:
        raise AwsElasticityTransitionError("ECS returned invalid worker state")
    return ElasticityTransitionObservation(
        observed_at=observed_at,
        application=application,
        source_queue=_queue_state(session, source_queue_url, runner=runner),
        dead_letter_queue=_queue_state(
            session,
            dead_letter_queue_url,
            runner=runner,
        ),
        worker_desired_count=counts[0],
        worker_running_count=counts[1],
        worker_pending_count=counts[2],
    )


def _native_autoscaling_state(
    session: AwsSession,
    *,
    expected: WorkerAutoscalingPolicy,
    runner: ProcessRunner,
    observed_at: datetime,
) -> WorkerAutoscalingVerification:
    common = (
        "--service-namespace",
        "ecs",
        "--resource-id",
        expected.resource_id,
        "--scalable-dimension",
        "ecs:service:DesiredCount",
        "--output",
        "json",
    )
    targets_result = invoke(
        runner,
        (
            *aws_prefix(session),
            "application-autoscaling",
            "describe-scalable-targets",
            "--service-namespace",
            "ecs",
            "--resource-ids",
            expected.resource_id,
            "--scalable-dimension",
            "ecs:service:DesiredCount",
            "--output",
            "json",
        ),
        action="worker scalable-target verification",
    )
    policies_result = invoke(
        runner,
        (
            *aws_prefix(session),
            "application-autoscaling",
            "describe-scaling-policies",
            *common,
        ),
        action="worker scaling-policy verification",
    )
    alarms_result = invoke(
        runner,
        (
            *aws_prefix(session),
            "cloudwatch",
            "describe-alarms",
            "--alarm-names",
            expected.high_alarm_name,
            expected.empty_alarm_name,
            "--output",
            "json",
        ),
        action="worker scaling-alarm verification",
    )
    try:
        targets = loads(targets_result.stdout)["ScalableTargets"]
        policies = loads(policies_result.stdout)["ScalingPolicies"]
        alarms = loads(alarms_result.stdout)["MetricAlarms"]
        if len(targets) != 1 or len(policies) != 2 or len(alarms) != 2:
            raise ValueError("incomplete native autoscaling state")
        target = targets[0]
        suspended_state = target.get("SuspendedState", {})
        policy_arns = {item["PolicyName"]: item["PolicyARN"] for item in policies}
        expected_alarm_actions = {
            expected.high_alarm_name: [policy_arns[expected.scale_out_policy_name]],
            expected.empty_alarm_name: [policy_arns[expected.scale_in_policy_name]],
        }
        normalized_policies = tuple(
            WorkerScalingPolicyVerification(
                name=item["PolicyName"],
                cooldown_seconds=item["StepScalingPolicyConfiguration"]["Cooldown"],
                scaling_adjustment=item["StepScalingPolicyConfiguration"][
                    "StepAdjustments"
                ][0]["ScalingAdjustment"],
                metric_interval_lower_bound=item["StepScalingPolicyConfiguration"][
                    "StepAdjustments"
                ][0].get("MetricIntervalLowerBound"),
                metric_interval_upper_bound=item["StepScalingPolicyConfiguration"][
                    "StepAdjustments"
                ][0].get("MetricIntervalUpperBound"),
            )
            for item in sorted(policies, key=lambda value: value["PolicyName"])
            if item["PolicyType"] == "StepScaling"
            and item["ResourceId"] == expected.resource_id
            and item["ScalableDimension"] == "ecs:service:DesiredCount"
            and item["ServiceNamespace"] == "ecs"
            and item["StepScalingPolicyConfiguration"]["AdjustmentType"]
            == "ExactCapacity"
            and item["StepScalingPolicyConfiguration"]["MetricAggregationType"]
            == "Maximum"
            and len(item["StepScalingPolicyConfiguration"]["StepAdjustments"]) == 1
        )
        normalized_alarms = []
        for item in sorted(alarms, key=lambda value: value["AlarmName"]):
            alarm_name = item["AlarmName"]
            if (
                item["AlarmActions"] != expected_alarm_actions[alarm_name]
                or item["OKActions"] != []
                or item["InsufficientDataActions"] != []
            ):
                continue
            signal_name = "visible_backlog"
            metric_query_ids: tuple[str, ...] = ()
            if alarm_name == expected.high_alarm_name:
                expected_metric_name = {
                    2: "ApproximateNumberOfMessagesVisible",
                    3: "NumberOfMessagesSent",
                    4: "ArrivalRate",
                }[expected.policy_version]
                expected_statistic = (
                    "Sum" if expected.policy_version == 3 else "Maximum"
                )
                if (
                    item.get("MetricName") != expected_metric_name
                    or item.get("Namespace")
                    != (
                        "TrackRelay/Elasticity"
                        if expected.policy_version == 4
                        else "AWS/SQS"
                    )
                    or item.get("Period")
                    != (
                        expected.scale_out_period_seconds
                        if expected.policy_version == 4
                        else expected.metric_period_seconds
                    )
                    or item.get("Statistic") != expected_statistic
                    or item.get("Dimensions")
                    != [{"Name": "QueueName", "Value": expected.queue_name}]
                ):
                    continue
                signal_name = {
                    2: "visible_backlog",
                    3: "messages_sent",
                    4: "arrival_rate",
                }[expected.policy_version]
            elif expected.policy_version >= 3:
                metrics = item.get("Metrics")
                if not isinstance(metrics, list):
                    continue
                metric_query_ids = tuple(sorted(query["Id"] for query in metrics))
                expected_metric_names = {
                    "sent": ("NumberOfMessagesSent", "Sum"),
                    "visible": ("ApproximateNumberOfMessagesVisible", "Maximum"),
                    "in_flight": (
                        "ApproximateNumberOfMessagesNotVisible",
                        "Maximum",
                    ),
                    "delayed": ("ApproximateNumberOfMessagesDelayed", "Maximum"),
                }
                query_by_id = {query["Id"]: query for query in metrics}
                expression = query_by_id.get("release_safe", {})
                if (
                    set(query_by_id) != {"release_safe", *expected_metric_names}
                    or expression.get("Expression")
                    != (
                        "IF(FILL(sent, 0) < 120, IF(FILL(visible, 0) + "
                        "FILL(in_flight, 0) + FILL(delayed, 0) < 10, 1, 0), 0)"
                    )
                    or expression.get("ReturnData") is not True
                ):
                    continue
                metric_queries_match = True
                for query_id, (metric_name, statistic) in expected_metric_names.items():
                    query = query_by_id[query_id]
                    metric_stat = query.get("MetricStat", {})
                    metric = metric_stat.get("Metric", {})
                    if (
                        query.get("ReturnData") is not False
                        or metric.get("MetricName") != metric_name
                        or metric.get("Namespace") != "AWS/SQS"
                        or metric.get("Dimensions")
                        != [{"Name": "QueueName", "Value": expected.queue_name}]
                        or metric_stat.get("Period") != expected.metric_period_seconds
                        or metric_stat.get("Stat") != statistic
                    ):
                        metric_queries_match = False
                        break
                if not metric_queries_match:
                    continue
                signal_name = "low_demand_and_queue_work"
            else:
                if (
                    item.get("MetricName") != "ApproximateNumberOfMessagesVisible"
                    or item.get("Namespace") != "AWS/SQS"
                    or item.get("Period") != expected.metric_period_seconds
                    or item.get("Statistic") != "Maximum"
                    or item.get("Dimensions")
                    != [{"Name": "QueueName", "Value": expected.queue_name}]
                ):
                    continue
            normalized_alarms.append(
                WorkerAlarmVerification(
                    name=alarm_name,
                    actions_enabled=item["ActionsEnabled"],
                    comparison_operator=item["ComparisonOperator"],
                    datapoints_to_alarm=item["DatapointsToAlarm"],
                    evaluation_periods=item["EvaluationPeriods"],
                    threshold=item["Threshold"],
                    treat_missing_data=item["TreatMissingData"],
                    signal=signal_name,
                    metric_query_ids=metric_query_ids,
                )
            )
        verification = WorkerAutoscalingVerification(
            collected_at=observed_at,
            resource_id=target["ResourceId"],
            minimum_capacity=target["MinCapacity"],
            maximum_capacity=target["MaxCapacity"],
            suspended=any(suspended_state.values()),
            policies=normalized_policies,
            alarms=tuple(normalized_alarms),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AwsElasticityTransitionError(
            "AWS returned incomplete worker autoscaling state"
        ) from error
    if not verification.matches(expected):
        raise AwsElasticityTransitionError(
            "native worker autoscaling differs from the frozen policy"
        )
    return verification


def verify_native_worker_autoscaling(
    session: AwsSession,
    *,
    expected: WorkerAutoscalingPolicy,
    runner: ProcessRunner,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: Timer = monotonic,
    sleeper: Sleeper = sleep,
    timeout_seconds: float = 120,
) -> WorkerAutoscalingVerification:
    """Wait boundedly for all native policy resources to become consistent."""
    deadline = timer() + timeout_seconds
    while True:
        try:
            return _native_autoscaling_state(
                session,
                expected=expected,
                runner=runner,
                observed_at=now(),
            )
        except AwsElasticityTransitionError:
            if timer() >= deadline:
                raise
            sleeper(5)


def _image_digests(manifest: dict[str, object]) -> dict[str, str]:
    images = manifest.get("async_images")
    if not isinstance(images, dict) or set(images) != set(IMAGE_ROLES):
        raise AwsElasticityTransitionError("session image evidence is invalid")
    result: dict[str, str] = {}
    for role in IMAGE_ROLES:
        item = images.get(role)
        digest = item.get("digest") if isinstance(item, dict) else None
        if (
            not isinstance(digest, str)
            or IMAGE_DIGEST_PATTERN.fullmatch(digest) is None
        ):
            raise AwsElasticityTransitionError("session image evidence is invalid")
        result[role] = digest
    return result


def _terraform_variables(
    session: AwsSession,
    *,
    image_digests: dict[str, str],
) -> tuple[str, ...]:
    return (
        *session.terraform_variables(),
        f"-var=api_image_digest={image_digests['api']}",
        f"-var=worker_image_digest={image_digests['worker']}",
        f"-var=simulator_image_digest={image_digests['simulator']}",
        "-var=async_services_enabled=true",
        "-var=worker_autoscaling_enabled=true",
    )


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


def validate_elasticity_transition_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> tuple[dict[str, object], ExperimentResetResult]:
    """Require the exact verified reset and unchanged session approval."""
    if not session.session_id.startswith("cloud-session-4-"):
        raise AwsSessionError("Stage 9.7 transition must use cloud session 4")
    if session.deployment_mode != "async":
        raise AwsSessionError("Stage 9.7 transition requires async mode")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "experiment_reset_verified":
        raise AwsSessionError("worker autoscaling requires a verified reset")
    recorded_ceiling = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded_ceiling, str) or parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    ) != parse_positive_money(recorded_ceiling, field_name="recorded cost ceiling"):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    reset = manifest.get("experiment_reset")
    if not isinstance(reset, dict) or reset.get("result") != (
        "elasticity/reset/result.json"
    ):
        raise AwsSessionError("verified reset result is not recorded")
    try:
        result = ExperimentResetResult.model_validate_json(
            (session.evidence_dir / reset["result"]).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise AwsSessionError("verified reset result is invalid") from error
    if (
        reset.get("fixed_test_run_id") != str(result.fixed_test_run_id)
        or not result.observations[-1].empty_and_fixed
    ):
        raise AwsSessionError("verified reset did not finish empty")
    return manifest, result


def execute_elasticity_transition(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    reset_result: ExperimentResetResult | None,
    runner: ProcessRunner = run_process,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    policy_verifier: Callable[..., WorkerAutoscalingVerification] = (
        verify_native_worker_autoscaling
    ),
    client: httpx.Client | None = None,
) -> ElasticityTransitionEvidence | DiagnosticTransitionEvidence:
    """Plan, apply, and natively prove the exact policy-only intervention."""
    diagnostic = reset_result is None
    if diagnostic:
        journal = manifest.get("elasticity_diagnostic_session")
        if (
            manifest.get("status") != "async_deployed"
            or not isinstance(journal, dict)
            or journal.get("kind") != "elastic-only-diagnostic"
            or journal.get("phase") != "transition"
            or journal.get("headline_eligible") is not False
            or any(key in manifest for key in ("fixed_control", "experiment_reset"))
        ):
            raise AwsSessionError(
                "diagnostic transition requires a fresh diagnostic deployment"
            )
    _current, revision = require_clean_approved_revision(session, runner=runner)
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
    source_queue_url = terraform_output(session, "delivery_queue_url", runner=runner)
    dead_letter_queue_url = terraform_output(
        session,
        "delivery_dead_letter_queue_url",
        runner=runner,
    )
    api_url = terraform_output(session, "async_api_url", runner=runner)
    if not isinstance(dimensions, dict) or not isinstance(capacity, dict):
        raise AwsElasticityTransitionError("Terraform returned invalid inputs")
    cluster_name = dimensions.get("cluster_name")
    worker_service_name = dimensions.get("worker_service_name")
    queue_name = dimensions.get("delivery_queue_name")
    if (
        not valid_async_observability_dimensions(dimensions)
        or not isinstance(cluster_name, str)
        or CLUSTER_NAME_PATTERN.fullmatch(cluster_name) is None
        or not isinstance(worker_service_name, str)
        or SERVICE_NAME_PATTERN.fullmatch(worker_service_name) is None
        or not isinstance(queue_name, str)
        or not queue_name.endswith("-delivery")
        or not isinstance(api_url, str)
        or not _valid_api_url(api_url)
        or not isinstance(source_queue_url, str)
        or not _valid_queue_url(source_queue_url, region=session.region)
        or not isinstance(dead_letter_queue_url, str)
        or not _valid_queue_url(dead_letter_queue_url, region=session.region)
        or set(capacity) != set(FIXED_SERVICE_CAPACITY)
    ):
        raise AwsElasticityTransitionError("Terraform returned invalid inputs")
    for role, expected_capacity in FIXED_SERVICE_CAPACITY.items():
        observed = capacity.get(role)
        if not isinstance(observed, dict):
            raise AwsElasticityTransitionError(
                "service capacity changed before autoscaling"
            )
        numeric = {
            key: observed.get(key)
            for key in ("cpu_units", "desired_count", "memory_mib")
        }
        service_name = observed.get("service_name")
        if (
            set(observed)
            != {"cpu_units", "desired_count", "memory_mib", "service_name"}
            or any(type(value) is not int for value in numeric.values())
            or numeric != expected_capacity
            or not isinstance(service_name, str)
            or SERVICE_NAME_PATTERN.fullmatch(service_name) is None
            or not service_name.endswith(f"-{role}")
        ):
            raise AwsElasticityTransitionError(
                "service capacity changed before autoscaling"
            )
    if (
        capacity["api"]["service_name"] != dimensions["api_service_name"]
        or capacity["simulator"]["service_name"] != dimensions["simulator_service_name"]
        or capacity["worker"]["service_name"] != dimensions["worker_service_name"]
    ):
        raise AwsElasticityTransitionError(
            "service identities changed before autoscaling"
        )
    expected_policy = _expected_policy(
        cluster_name=cluster_name,
        worker_service_name=worker_service_name,
        queue_name=queue_name,
    )
    relative_root = (
        "elasticity/diagnostic/transition" if diagnostic else "elasticity/transition"
    )
    evidence_root = session.evidence_dir / relative_root
    evidence_root.mkdir(parents=True, exist_ok=False)
    client_context = (
        client if client is not None else httpx.Client(base_url=api_url, timeout=10)
    )
    close_client = client is None
    try:
        pre_apply = _transition_observation(
            session,
            client=client_context,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            runner=runner,
            observed_at=now(),
        )
        if not pre_apply.empty_at_minimum:
            raise AwsElasticityTransitionError(
                "pre-transition state is not empty at minimum worker capacity"
            )
        (evidence_root / "pre-apply.json").write_text(
            pre_apply.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        image_digests = _image_digests(manifest)
        plan_path = evidence_root / "terraform-autoscaling.tfplan"
        plan_result = runner(
            session.terraform_command(
                "plan",
                "-input=false",
                f"-out={plan_path.resolve()}",
                *_terraform_variables(session, image_digests=image_digests),
            ),
            None,
        )
        write_command_log(evidence_root / "terraform-plan.log", plan_result)
        if plan_result.returncode != 0:
            raise AwsElasticityTransitionError("Terraform autoscaling plan failed")
        if not plan_path.is_file():
            raise AwsElasticityTransitionError(
                "Terraform did not save the autoscaling plan"
            )
        plan_digest = file_sha256(plan_path)
        show_result = invoke(
            runner,
            session.terraform_command("show", "-json", plan_path.resolve().as_posix()),
            action="Terraform autoscaling plan inspection",
        )
        plan_evidence = validate_elasticity_transition_plan(
            show_result.stdout,
            plan_sha256=plan_digest,
            expected_policy=expected_policy,
        )
        (evidence_root / "plan-evidence.json").write_text(
            plan_evidence.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        manifest["worker_autoscaling_transition"] = {
            "git_revision": revision,
            "plan": f"{relative_root}/plan-evidence.json",
            "plan_sha256": plan_digest,
            "unconditional_teardown_armed": True,
        }
        manifest["status"] = "worker_autoscaling_planned"
        write_manifest(session, manifest)
        if file_sha256(plan_path) != plan_digest:
            raise AwsElasticityTransitionError("saved autoscaling plan changed")
        apply_result = runner(
            session.terraform_command(
                "apply",
                "-input=false",
                plan_path.resolve().as_posix(),
            ),
            None,
        )
        write_command_log(evidence_root / "terraform-apply.log", apply_result)
        if apply_result.returncode != 0:
            raise AwsElasticityTransitionError("Terraform autoscaling apply failed")
        applied_at = now()
        manifest = load_manifest(session)
        manifest["status"] = "worker_autoscaling_applied_pending_verification"
        manifest["worker_autoscaling_transition"]["applied_at"] = applied_at.isoformat()
        write_manifest(session, manifest)

        observed_policy = terraform_output(
            session,
            "worker_autoscaling_policy",
            runner=runner,
            json_output=True,
        )
        try:
            output_policy = WorkerAutoscalingPolicy.model_validate(observed_policy)
        except ValueError as error:
            raise AwsElasticityTransitionError(
                "Terraform returned invalid applied autoscaling policy"
            ) from error
        if output_policy != expected_policy:
            raise AwsElasticityTransitionError(
                "applied autoscaling policy differs from the frozen policy"
            )
        invoke(
            runner,
            (
                *aws_prefix(session),
                "ecs",
                "wait",
                "services-stable",
                "--cluster",
                cluster_name,
                "--services",
                worker_service_name,
            ),
            action="worker service stability after autoscaling apply",
        )
        native = policy_verifier(
            session,
            expected=expected_policy,
            runner=runner,
        )
        post_apply = _transition_observation(
            session,
            client=client_context,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            runner=runner,
            observed_at=now(),
        )
        evidence_type = (
            DiagnosticTransitionEvidence if diagnostic else ElasticityTransitionEvidence
        )
        evidence = evidence_type(
            reset_fixed_test_run_id=(
                reset_result.fixed_test_run_id if reset_result is not None else None
            ),
            policy=expected_policy,
            plan=plan_evidence,
            pre_apply=pre_apply,
            post_apply=post_apply,
            native=native,
            applied_at=applied_at,
            verified_at=now(),
        )
        (evidence_root / "evidence.json").write_text(
            evidence.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return evidence
    finally:
        if close_client:
            client_context.close()


def run_elasticity_transition_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    transition_runner: TransitionRunner = execute_elasticity_transition,
    runner: ProcessRunner = run_process,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
) -> ElasticityTransitionEvidence:
    """Enable only worker autoscaling or destroy and verify the session."""
    manifest, reset_result = validate_elasticity_transition_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    try:
        evidence = transition_runner(
            session,
            manifest=manifest,
            reset_result=reset_result,
            runner=runner,
        )
        manifest = load_manifest(session)
        manifest["worker_autoscaling_transition"] = {
            **manifest["worker_autoscaling_transition"],
            "evidence": "elasticity/transition/evidence.json",
            "maximum_capacity": evidence.policy.maximum_capacity,
            "minimum_capacity": evidence.policy.minimum_capacity,
        }
        manifest["status"] = "worker_autoscaling_verified"
        write_manifest(session, manifest)
        return evidence
    except BaseException as workflow_error:
        operator_failure("Phase transition; beginning cleanup", workflow_error)
        cleanup_errors = []
        try:
            destroyer(session)
        except BaseException as error:  # noqa: BLE001 - still verify natively
            cleanup_errors.append(error)
        try:
            teardown_verifier(session)
        except BaseException as error:  # noqa: BLE001 - report all cleanup failures
            cleanup_errors.append(error)
        if cleanup_errors:
            raise AwsElasticityTransitionCleanupError(
                "Stage 9.7 autoscaling transition failed and cleanup did not complete",
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
    run_elasticity_transition_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during Stage 9.7 transition")


def main(argv: Sequence[str] | None = None) -> int:
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsAsyncDeploymentError,
        AwsElasticityTransitionCleanupError,
        AwsElasticityTransitionError,
        AwsSessionError,
        KeyboardInterrupt,
        httpx.HTTPError,
    ) as error:
        raise SystemExit(f"AWS elasticity transition failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
