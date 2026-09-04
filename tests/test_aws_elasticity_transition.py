"""Tests for the policy-only Stage 9.7 elasticity transition."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import dumps
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import httpx
from pytest import MonkeyPatch, mark, raises

from trackrelay.aws_elasticity_transition import (
    AwsElasticityTransitionError,
    ElasticityTransitionEvidence,
    ElasticityTransitionObservation,
    ElasticityTransitionPlanEvidence,
    WorkerAlarmVerification,
    WorkerAutoscalingPolicy,
    WorkerAutoscalingVerification,
    WorkerScalingPolicyVerification,
    execute_elasticity_transition,
    run_elasticity_transition_session,
    validate_elasticity_transition_approval,
    validate_elasticity_transition_plan,
    verify_native_worker_autoscaling,
)
from trackrelay.aws_experiment_reset import (
    ExperimentResetObservation,
    ExperimentResetResult,
    ResetQueueState,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)
from trackrelay.downstream.control import SimulatorMode
from trackrelay.services.experiment_reset import (
    ExperimentDatabaseCounts,
    ExperimentResetEvidence,
    ExperimentStateSnapshot,
)

SESSION_ID = "cloud-session-4-20260903T120000Z"
GIT_REVISION = "f" * 40
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000949")
STARTED_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
API_URL = "http://trackrelay-elastic.example.com"
SOURCE_QUEUE_URL = (
    "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-8a7e37db-delivery"
)
DLQ_URL = (
    "https://sqs.ap-southeast-3.amazonaws.com/123456789012/"
    "trackrelay-8a7e37db-delivery-dlq"
)
CLUSTER_NAME = "trackrelay-8a7e37db-async"
WORKER_SERVICE = "trackrelay-8a7e37db-worker"
QUEUE_NAME = "trackrelay-8a7e37db-delivery"
EMPTY_QUEUE = ResetQueueState(
    visible_messages=0,
    in_flight_messages=0,
    delayed_messages=0,
)


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, "")


def policy(version: int = 6) -> WorkerAutoscalingPolicy:
    return WorkerAutoscalingPolicy(
        policy_version=version,
        scale_in_evaluation_periods=1,
        scale_in_period_seconds=10,
        scale_in_quiet_seconds=120 if version == 6 else 180,
        backlog_threshold_messages=None,
        empty_alarm_name=f"{WORKER_SERVICE}-release-safe",
        high_alarm_name=f"{WORKER_SERVICE}-demand-high",
        queue_name=QUEUE_NAME,
        resource_id=f"service/{CLUSTER_NAME}/{WORKER_SERVICE}",
        scale_in_policy_name=f"{WORKER_SERVICE}-scale-in",
        scale_in_messages_per_minute=120,
        scale_in_queue_work_threshold=10,
        scale_out_policy_name=f"{WORKER_SERVICE}-scale-out",
        scale_out_period_seconds=10,
        scale_out_rate_per_second=3,
        scale_out_evaluation_periods=2,
    )


def test_candidate_v2_policy_evidence_remains_readable() -> None:
    historical = WorkerAutoscalingPolicy.model_validate(
        {
            "empty_alarm_name": f"{WORKER_SERVICE}-empty",
            "high_alarm_name": f"{WORKER_SERVICE}-backlog-high",
            "queue_name": QUEUE_NAME,
            "resource_id": f"service/{CLUSTER_NAME}/{WORKER_SERVICE}",
            "scale_in_policy_name": f"{WORKER_SERVICE}-scale-in",
            "scale_out_policy_name": f"{WORKER_SERVICE}-scale-out",
        }
    )

    assert historical.policy_version == 2
    assert historical.backlog_threshold_messages == 10
    assert historical.scale_out_messages_per_minute is None


def test_candidate_v3_policy_evidence_remains_readable() -> None:
    historical = policy().model_dump(
        exclude={
            "scale_out_period_seconds",
            "scale_out_rate_per_second",
            "scale_in_period_seconds",
            "scale_in_quiet_seconds",
        }
    )
    historical.update(
        scale_in_evaluation_periods=3,
        policy_version=3,
        scale_out_evaluation_periods=1,
        scale_out_messages_per_minute=300,
    )
    parsed = WorkerAutoscalingPolicy.model_validate(historical)
    assert parsed.policy_version == 3
    assert parsed.scale_out_period_seconds is None


@mark.parametrize("version,seconds", ((5, 180), (6, 120)))
def test_quiet_period_is_versioned_without_changing_other_controls(version, seconds):
    fields = {
        **policy().model_dump(),
        "policy_version": version,
        "scale_in_quiet_seconds": seconds,
    }
    parsed = WorkerAutoscalingPolicy.model_validate(fields)
    assert parsed.scale_in_quiet_seconds == seconds
    with raises(ValueError, match="quiet period"):
        WorkerAutoscalingPolicy.model_validate(
            {**fields, "scale_in_quiet_seconds": 120 if seconds == 180 else 180}
        )
    assert parsed.scale_out_rate_per_second == 3
    assert parsed.scale_out_evaluation_periods == 2
    assert parsed.scale_in_cooldown_seconds == 60
    assert parsed.scale_in_queue_work_threshold == 10


@mark.parametrize(
    "field,value",
    [
        ("scale_out_period_seconds", None),
        ("scale_out_evaluation_periods", 1),
        ("scale_out_rate_per_second", None),
    ],
)
def test_high_resolution_policy_rejects_incomplete_contract(field, value):
    from pydantic import ValidationError

    with raises(ValidationError):
        WorkerAutoscalingPolicy.model_validate({**policy().model_dump(), field: value})


def empty_state() -> ExperimentStateSnapshot:
    return ExperimentStateSnapshot(
        database=ExperimentDatabaseCounts(
            delivery_attempts=0,
            delivery_outbox_entries=0,
            events=0,
            shipments=0,
            test_runs=0,
        ),
        simulator_receipts=0,
        simulator_mode=SimulatorMode.HEALTHY,
    )


def reset_result() -> ExperimentResetResult:
    populated = ExperimentStateSnapshot(
        database=ExperimentDatabaseCounts(
            delivery_attempts=2,
            delivery_outbox_entries=2,
            events=2,
            shipments=2,
            test_runs=1,
        ),
        simulator_receipts=2,
        simulator_mode=SimulatorMode.HEALTHY,
    )
    reset = ExperimentResetEvidence(
        test_run_id=TEST_RUN_ID,
        expected_event_count=2,
        before=populated,
        after=empty_state(),
        simulator_receipts_removed=2,
    )
    observations = tuple(
        ExperimentResetObservation(
            observed_at=STARTED_AT + timedelta(seconds=seconds),
            seconds_after_purge=seconds,
            application=empty_state(),
            source_queue=EMPTY_QUEUE,
            dead_letter_queue=EMPTY_QUEUE,
            worker_desired_count=1,
            worker_running_count=1,
            worker_pending_count=0,
        )
        for seconds in (60, 90)
    )
    return ExperimentResetResult(
        fixed_test_run_id=TEST_RUN_ID,
        started_at=STARTED_AT,
        completed_at=STARTED_AT + timedelta(seconds=90),
        application_reset=reset,
        observations=observations,
        autoscaling_target_absent_before=True,
        autoscaling_target_absent_after=True,
    )


def ready_session(
    tmp_path: Path,
    *,
    status: str = "experiment_reset_verified",
) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile="trackrelay-admin",
        region="ap-southeast-3",
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        deployment_mode="async",
    )
    reset_dir = session.evidence_dir / "elasticity" / "reset"
    reset_dir.mkdir(parents=True)
    result = reset_result()
    (reset_dir / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    images = {
        role: {"digest": f"sha256:{character * 64}"}
        for role, character in (("api", "a"), ("worker", "b"), ("simulator", "c"))
    }
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "5.00",
            "async_images": images,
            "deployment_mode": "async",
            "experiment_reset": {
                "fixed_test_run_id": str(TEST_RUN_ID),
                "result": "elasticity/reset/result.json",
            },
            "git_revision": GIT_REVISION,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": session.rehost_instance_type,
            "session_id": session.session_id,
            "status": status,
        },
    )
    return session


def approvals(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "5.0",
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def plan_document(expected: WorkerAutoscalingPolicy) -> dict[str, object]:
    common = {
        "resource_id": expected.resource_id,
        "scalable_dimension": "ecs:service:DesiredCount",
        "service_namespace": "ecs",
    }

    def resource(address: str, after: dict[str, object]) -> dict[str, object]:
        return {
            "address": address,
            "change": {"actions": ["create"], "before": None, "after": after},
        }

    resources = [
        resource(
            "aws_appautoscaling_target.async_worker[0]",
            {**common, "min_capacity": 1, "max_capacity": 8},
        ),
        resource(
            "aws_appautoscaling_policy.async_worker_scale_out[0]",
            {
                **common,
                "name": expected.scale_out_policy_name,
                "policy_type": "StepScaling",
                "step_scaling_policy_configuration": [
                    {
                        "adjustment_type": "ExactCapacity",
                        "cooldown": 60,
                        "metric_aggregation_type": "Maximum",
                        "step_adjustment": [
                            {
                                "metric_interval_lower_bound": 0,
                                "scaling_adjustment": 8,
                            }
                        ],
                    }
                ],
            },
        ),
        resource(
            "aws_appautoscaling_policy.async_worker_scale_in[0]",
            {
                **common,
                "name": expected.scale_in_policy_name,
                "policy_type": "StepScaling",
                "step_scaling_policy_configuration": [
                    {
                        "adjustment_type": "ExactCapacity",
                        "cooldown": 60,
                        "metric_aggregation_type": "Maximum",
                        "step_adjustment": [
                            {
                                "metric_interval_lower_bound": 0,
                                "scaling_adjustment": 1,
                            }
                        ],
                    }
                ],
            },
        ),
        resource(
            "aws_cloudwatch_metric_alarm.async_worker_backlog_high[0]",
            {
                "actions_enabled": True,
                "alarm_name": expected.high_alarm_name,
                "comparison_operator": "GreaterThanOrEqualToThreshold",
                "datapoints_to_alarm": 2,
                "dimensions": {"QueueName": expected.queue_name},
                "evaluation_periods": 2,
                "metric_name": "ArrivalRate",
                "namespace": "TrackRelay/Elasticity",
                "period": 10,
                "statistic": "Maximum",
                "threshold": 3,
                "treat_missing_data": "notBreaching",
            },
        ),
        resource(
            "aws_cloudwatch_metric_alarm.async_worker_empty[0]",
            {
                "actions_enabled": True,
                "alarm_name": expected.empty_alarm_name,
                "comparison_operator": "GreaterThanOrEqualToThreshold",
                "datapoints_to_alarm": 1,
                "evaluation_periods": 1,
                "metric_query": [
                    {
                        "id": "release_safe",
                        "expression": "FILL(quiet, 0)",
                        "period": 10,
                        "return_data": True,
                    },
                    *(
                        {
                            "id": query_id,
                            "return_data": False,
                            "metric": [
                                {
                                    "dimensions": {"QueueName": expected.queue_name},
                                    "metric_name": metric_name,
                                    "namespace": "TrackRelay/Elasticity",
                                    "period": 10,
                                    "stat": statistic,
                                }
                            ],
                        }
                        for query_id, metric_name, statistic in (
                            ("quiet", "QuietSeconds", "Minimum"),
                        )
                    ),
                ],
                "threshold": expected.scale_in_quiet_seconds,
                "treat_missing_data": "notBreaching",
            },
        ),
    ]
    return {
        "resource_changes": resources,
        "output_changes": {
            "worker_autoscaling_policy": {
                "actions": ["create"],
                "before": None,
                "after": expected.model_dump(mode="json"),
            }
        },
    }


def native_verification(
    expected: WorkerAutoscalingPolicy,
) -> WorkerAutoscalingVerification:
    return WorkerAutoscalingVerification(
        collected_at=STARTED_AT + timedelta(minutes=2),
        resource_id=expected.resource_id,
        minimum_capacity=1,
        maximum_capacity=8,
        suspended=False,
        policies=(
            WorkerScalingPolicyVerification(
                name=expected.scale_in_policy_name,
                cooldown_seconds=60,
                scaling_adjustment=1,
                metric_interval_lower_bound=0,
            ),
            WorkerScalingPolicyVerification(
                name=expected.scale_out_policy_name,
                cooldown_seconds=60,
                scaling_adjustment=8,
                metric_interval_lower_bound=0,
            ),
        ),
        alarms=(
            WorkerAlarmVerification(
                name=expected.empty_alarm_name,
                actions_enabled=True,
                comparison_operator="GreaterThanOrEqualToThreshold",
                datapoints_to_alarm=1,
                evaluation_periods=1,
                threshold=expected.scale_in_quiet_seconds,
                treat_missing_data="notBreaching",
                signal="quiet_seconds",
                metric_query_ids=("quiet", "release_safe"),
            ),
            WorkerAlarmVerification(
                name=expected.high_alarm_name,
                actions_enabled=True,
                comparison_operator="GreaterThanOrEqualToThreshold",
                datapoints_to_alarm=2,
                evaluation_periods=2,
                threshold=3,
                treat_missing_data="notBreaching",
                signal="arrival_rate",
            ),
        ),
    )


def transition_observation() -> ElasticityTransitionObservation:
    return ElasticityTransitionObservation(
        observed_at=STARTED_AT + timedelta(minutes=2),
        application=empty_state(),
        source_queue=EMPTY_QUEUE,
        dead_letter_queue=EMPTY_QUEUE,
        worker_desired_count=1,
        worker_running_count=1,
        worker_pending_count=0,
    )


def transition_evidence() -> ElasticityTransitionEvidence:
    expected = policy()
    return ElasticityTransitionEvidence(
        reset_fixed_test_run_id=TEST_RUN_ID,
        policy=expected,
        plan=ElasticityTransitionPlanEvidence(
            plan_sha256="1" * 64,
            resource_addresses=(
                "aws_appautoscaling_policy.async_worker_scale_in[0]",
                "aws_appautoscaling_policy.async_worker_scale_out[0]",
                "aws_appautoscaling_target.async_worker[0]",
                "aws_cloudwatch_metric_alarm.async_worker_backlog_high[0]",
                "aws_cloudwatch_metric_alarm.async_worker_empty[0]",
            ),
            resource_actions=("create",) * 5,
            output_action="create",
        ),
        pre_apply=transition_observation(),
        post_apply=transition_observation(),
        native=native_verification(expected),
        applied_at=STARTED_AT + timedelta(minutes=1),
        verified_at=STARTED_AT + timedelta(minutes=2),
    )


def test_transition_requires_the_verified_reset(tmp_path: Path) -> None:
    session = ready_session(tmp_path, status="fixed_control_qualified")

    with raises(AwsSessionError, match="verified reset"):
        validate_elasticity_transition_approval(session, **approvals(session))


@mark.parametrize("version", (5, 6))
def test_plan_allows_only_the_five_policy_resources(version) -> None:
    expected = policy(version)
    document = plan_document(expected)

    evidence = validate_elasticity_transition_plan(
        dumps(document),
        plan_sha256="2" * 64,
        expected_policy=expected,
    )

    assert evidence.resource_addresses[0].startswith("aws_appautoscaling_policy")
    document["resource_changes"].append(
        {
            "address": "aws_ecs_service.async_api[0]",
            "change": {"actions": ["update"], "before": {}, "after": {}},
        }
    )
    with raises(
        AwsElasticityTransitionError,
        match=r"unexpected: aws_ecs_service\.async_api\[0\] \(update\)",
    ):
        validate_elasticity_transition_plan(
            dumps(document),
            plan_sha256="2" * 64,
            expected_policy=expected,
        )


def test_plan_reports_cloud_map_replacement_without_plan_values() -> None:
    expected = policy()
    document = plan_document(expected)
    document["resource_changes"].extend(
        [
            {
                "address": "aws_ecs_service.async_simulator[0]",
                "change": {
                    "actions": ["update"],
                    "before": {"service_registries": "old-sensitive-value"},
                    "after": {"service_registries": "new-sensitive-value"},
                },
            },
            {
                "address": "aws_service_discovery_service.simulator[0]",
                "change": {
                    "actions": ["delete", "create"],
                    "before": {"health_check_custom_config": []},
                    "after": {
                        "health_check_custom_config": [
                            {"failure_threshold": "sensitive-value"}
                        ]
                    },
                },
            },
        ]
    )

    with raises(AwsElasticityTransitionError) as captured:
        validate_elasticity_transition_plan(
            dumps(document),
            plan_sha256="2" * 64,
            expected_policy=expected,
        )

    message = str(captured.value)
    assert "aws_ecs_service.async_simulator[0] (update)" in message
    assert "aws_service_discovery_service.simulator[0] (delete/create)" in message
    assert "sensitive-value" not in message
    assert "old-sensitive-value" not in message
    assert "new-sensitive-value" not in message


def test_plan_rejects_non_string_actions_as_invalid() -> None:
    expected = policy()
    document = plan_document(expected)
    document["resource_changes"][0]["change"]["actions"] = ["create", 1]

    with raises(AwsElasticityTransitionError, match="change is invalid"):
        validate_elasticity_transition_plan(
            dumps(document),
            plan_sha256="2" * 64,
            expected_policy=expected,
        )


@mark.parametrize(
    "field,value",
    [
        ("threshold", 100),
        ("period", 60),
        ("statistic", "Sum"),
        ("namespace", "AWS/SQS"),
        ("datapoints_to_alarm", 1),
    ],
)
def test_plan_rejects_a_changed_scale_out_threshold(field, value) -> None:
    expected = policy()
    document = plan_document(expected)
    high_alarm = next(
        item
        for item in document["resource_changes"]
        if item["address"].endswith("async_worker_backlog_high[0]")
    )
    high_alarm["change"]["after"][field] = value

    with raises(AwsElasticityTransitionError, match="frozen policy"):
        validate_elasticity_transition_plan(
            dumps(document),
            plan_sha256="2" * 64,
            expected_policy=expected,
        )


@mark.parametrize("version", (5, 6))
def test_native_verification_requires_alarm_policy_wiring(
    tmp_path: Path, version
) -> None:
    session = ready_session(tmp_path)
    expected = policy(version)
    scale_in_arn = "arn:aws:autoscaling:region:account:policy/scale-in"
    scale_out_arn = "arn:aws:autoscaling:region:account:policy/scale-out"

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        if "describe-scalable-targets" in call:
            return completed(
                call,
                stdout=dumps(
                    {
                        "ScalableTargets": [
                            {
                                "ResourceId": expected.resource_id,
                                "MinCapacity": 1,
                                "MaxCapacity": 8,
                                "SuspendedState": {},
                            }
                        ]
                    }
                ),
            )
        if "describe-scaling-policies" in call:
            policies = []
            for name, arn, capacity in (
                (expected.scale_in_policy_name, scale_in_arn, 1),
                (expected.scale_out_policy_name, scale_out_arn, 8),
            ):
                policies.append(
                    {
                        "PolicyName": name,
                        "PolicyARN": arn,
                        "PolicyType": "StepScaling",
                        "ResourceId": expected.resource_id,
                        "ScalableDimension": "ecs:service:DesiredCount",
                        "ServiceNamespace": "ecs",
                        "StepScalingPolicyConfiguration": {
                            "AdjustmentType": "ExactCapacity",
                            "Cooldown": 60,
                            "MetricAggregationType": "Maximum",
                            "StepAdjustments": [
                                {
                                    "MetricIntervalLowerBound": 0,
                                    "ScalingAdjustment": capacity,
                                }
                            ],
                        },
                    }
                )
            return completed(call, stdout=dumps({"ScalingPolicies": policies}))
        if "describe-alarms" in call:
            common_alarm = {
                "ActionsEnabled": True,
                "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                "OKActions": [],
                "InsufficientDataActions": [],
                "TreatMissingData": "notBreaching",
            }
            alarms = [
                {
                    **common_alarm,
                    "AlarmName": expected.high_alarm_name,
                    "AlarmActions": [scale_out_arn],
                    "DatapointsToAlarm": 2,
                    "Dimensions": [{"Name": "QueueName", "Value": QUEUE_NAME}],
                    "EvaluationPeriods": 2,
                    "MetricName": "ArrivalRate",
                    "Namespace": "TrackRelay/Elasticity",
                    "Period": 10,
                    "Statistic": "Maximum",
                    "Threshold": 3,
                },
                {
                    **common_alarm,
                    "AlarmName": expected.empty_alarm_name,
                    "AlarmActions": [scale_in_arn],
                    "DatapointsToAlarm": 1,
                    "EvaluationPeriods": 1,
                    "Threshold": expected.scale_in_quiet_seconds,
                    "Metrics": [
                        {
                            "Id": "release_safe",
                            "Expression": "FILL(quiet, 0)",
                            "Period": 10,
                            "ReturnData": True,
                        },
                        *(
                            {
                                "Id": query_id,
                                "ReturnData": False,
                                "MetricStat": {
                                    "Metric": {
                                        "Dimensions": [
                                            {"Name": "QueueName", "Value": QUEUE_NAME}
                                        ],
                                        "MetricName": metric_name,
                                        "Namespace": "TrackRelay/Elasticity",
                                    },
                                    "Period": 10,
                                    "Stat": statistic,
                                },
                            }
                            for query_id, metric_name, statistic in (
                                ("quiet", "QuietSeconds", "Minimum"),
                            )
                        ),
                    ],
                },
            ]
            return completed(call, stdout=dumps({"MetricAlarms": alarms}))
        raise AssertionError(call)

    verified = verify_native_worker_autoscaling(
        session,
        expected=expected,
        runner=runner,
        timeout_seconds=0,
    )

    assert verified.matches(expected)


@mark.parametrize("diagnostic", (False, True))
@mark.parametrize("high_resolution", (False, True))
def test_transition_applies_the_saved_policy_only_plan(
    tmp_path: Path, diagnostic, high_resolution
) -> None:
    session = ready_session(tmp_path)
    if diagnostic:
        manifest = load_manifest(session)
        manifest.pop("experiment_reset")
        manifest["status"] = "async_deployed"
        manifest["elasticity_diagnostic_session"] = {
            "kind": "elastic-only-diagnostic",
            "phase": "transition",
            "headline_eligible": False,
        }
        write_manifest(session, manifest)
    expected = policy()
    calls: list[tuple[str, ...]] = []
    plan_json = dumps(plan_document(expected))

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        calls.append(call)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        if len(call) >= 5 and call[2] == "output":
            name = call[4]
            outputs = {
                "async_observability_dimensions": {
                    **(
                        {"scaling_metrics_namespace": "TrackRelay/Elasticity"}
                        if high_resolution
                        else {}
                    ),
                    "api_service_name": "trackrelay-8a7e37db-api",
                    "cluster_name": CLUSTER_NAME,
                    "dashboard_name": "trackrelay-8a7e37db-async",
                    "dead_letter_queue_name": "trackrelay-8a7e37db-delivery-dlq",
                    "delivery_queue_name": QUEUE_NAME,
                    "load_balancer_dimension": "app/example/123",
                    "rds_identifier": "trackrelay-example",
                    "simulator_service_name": "trackrelay-8a7e37db-simulator",
                    "worker_service_name": WORKER_SERVICE,
                },
                "async_service_capacity": {
                    "api": {
                        "cpu_units": 1024,
                        "desired_count": 2,
                        "memory_mib": 2048,
                        "service_name": "trackrelay-8a7e37db-api",
                    },
                    "simulator": {
                        "cpu_units": 256,
                        "desired_count": 1,
                        "memory_mib": 512,
                        "service_name": "trackrelay-8a7e37db-simulator",
                    },
                    "worker": {
                        "cpu_units": 256,
                        "desired_count": 1,
                        "memory_mib": 512,
                        "service_name": WORKER_SERVICE,
                    },
                },
                "delivery_queue_url": SOURCE_QUEUE_URL,
                "delivery_dead_letter_queue_url": DLQ_URL,
                "async_api_url": API_URL,
                "worker_autoscaling_policy": expected.model_dump(mode="json"),
            }
            value = outputs[name]
            return completed(
                call,
                stdout=dumps(value) if call[3] == "-json" else str(value),
            )
        if call[2] == "plan":
            output_argument = next(item for item in call if item.startswith("-out="))
            Path(output_argument.removeprefix("-out=")).write_bytes(b"saved-plan")
            return completed(call)
        if call[2] == "show":
            return completed(call, stdout=plan_json)
        if call[2] == "apply":
            return completed(call)
        if "get-queue-attributes" in call:
            names = call[call.index("--attribute-names") + 1 : call.index("--query")]
            return completed(call, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in call:
            return completed(call, stdout="[1, 1, 0]")
        if "services-stable" in call:
            return completed(call)
        raise AssertionError(call)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=empty_state().model_dump(mode="json"),
            request=request,
        )

    with httpx.Client(
        base_url=API_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        evidence = execute_elasticity_transition(
            session,
            manifest=load_manifest(session),
            reset_result=None if diagnostic else reset_result(),
            runner=runner,
            now=lambda: STARTED_AT + timedelta(minutes=2),
            policy_verifier=lambda *_args, **_kwargs: native_verification(expected),
            client=client,
        )

    plan_call = next(call for call in calls if len(call) > 2 and call[2] == "plan")
    assert "-var=worker_autoscaling_enabled=true" in plan_call
    apply_call = next(call for call in calls if len(call) > 2 and call[2] == "apply")
    assert apply_call[-1].endswith("terraform-autoscaling.tfplan")
    assert evidence.policy.maximum_capacity == 8
    path = "elasticity/diagnostic/transition" if diagnostic else "elasticity/transition"
    assert (session.evidence_dir / path / "evidence.json").is_file()
    assert (evidence.reset_fixed_test_run_id is None) is diagnostic


def test_null_reset_cannot_bypass_paired_transition_approval(tmp_path):
    session = ready_session(tmp_path)
    calls = []
    with raises(AwsSessionError, match="fresh diagnostic deployment"):
        execute_elasticity_transition(
            session,
            manifest=load_manifest(session),
            reset_result=None,
            runner=lambda *args: calls.append(args),
        )
    assert not calls


def test_success_leaves_stack_for_elastic_replay(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = ready_session(tmp_path)
    expected = transition_evidence()
    cleanup: list[str] = []
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_transition.validate_elasticity_transition_approval",
        lambda *_args, **_kwargs: (load_manifest(session), reset_result()),
    )

    def transition_runner(current_session: AwsSession, **_kwargs):
        manifest = load_manifest(current_session)
        manifest["worker_autoscaling_transition"] = {
            "plan": "elasticity/transition/plan-evidence.json"
        }
        write_manifest(current_session, manifest)
        return expected

    result = run_elasticity_transition_session(
        session,
        **approvals(session),
        transition_runner=transition_runner,
        destroyer=lambda _session: cleanup.append("destroy"),
        teardown_verifier=lambda _session: cleanup.append("verify"),
    )

    assert result == expected
    assert cleanup == []
    manifest = load_manifest(session)
    assert manifest["status"] == "worker_autoscaling_verified"
    assert manifest["worker_autoscaling_transition"]["maximum_capacity"] == 8


def test_transition_failure_destroys_and_verifies(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = ready_session(tmp_path)
    cleanup: list[str] = []
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_transition.validate_elasticity_transition_approval",
        lambda *_args, **_kwargs: (load_manifest(session), reset_result()),
    )

    with raises(RuntimeError, match="simulated transition failure"):
        run_elasticity_transition_session(
            session,
            **approvals(session),
            transition_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated transition failure")
            ),
            destroyer=lambda _session: cleanup.append("destroy"),
            teardown_verifier=lambda _session: cleanup.append("verify"),
        )

    assert cleanup == ["destroy", "verify"]
