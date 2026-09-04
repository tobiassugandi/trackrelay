"""Offline tests for the elastic replay, qualification, and final teardown."""

from dataclasses import replace
from datetime import timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import TimeoutExpired
from uuid import UUID

import httpx
from pytest import MonkeyPatch, mark, raises

from tests.test_aws_elasticity_transition import (
    API_URL,
    DLQ_URL,
    SOURCE_QUEUE_URL,
    approvals,
    empty_state,
    native_verification,
    policy,
    transition_evidence,
)
from tests.test_aws_elasticity_transition import (
    ready_session as transition_session,
)
from tests.test_aws_fixed_control import (
    cloudwatch_evidence,
    completed,
    controller_runner,
    observation,
    passing_result,
)
from trackrelay.aws_elastic_treatment import (
    ELASTIC_TREATMENT_CONTRACT,
    AwsElasticTreatmentCleanupError,
    AwsElasticTreatmentError,
    ElasticTreatmentSummary,
    evaluate_elastic_treatment,
    run_elastic_treatment_session,
    validate_elastic_treatment_approval,
    verify_elastic_environment,
)
from trackrelay.aws_elasticity_cloudwatch import (
    ElasticityCloudWatchDatapoint,
    expected_bucket_starts,
)
from trackrelay.aws_experiment_reset import validate_experiment_reset_approval
from trackrelay.aws_fixed_control import (
    AwsFixedControlError,
    ElasticityResult,
    FixedControlResult,
    FixedControlSummary,
    evaluate_fixed_control_qualification,
    evaluate_treatment_guardrails,
    execute_elasticity_workload,
    run_fixed_control_session,
)
from trackrelay.aws_session import AwsSessionError, load_manifest, write_manifest
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    ElasticityWorkloadDefinition,
)

ELASTIC_ID = UUID("00000000-0000-0000-0000-000000000947")


def elastic_result():
    fixed = passing_result()
    start = fixed.load_started_at + timedelta(days=2)
    observations = []
    for seconds in range(0, fixed.definition.duration_seconds + 191, 10):
        remaining = seconds
        persisted = 0
        name, rate = "post-load", 0
        for step in fixed.definition.steps:
            persisted += (
                min(remaining, step.duration_seconds) * step.offered_rate_per_second
            )
            if remaining < step.duration_seconds:
                name, rate = step.name, step.offered_rate_per_second
                break
            remaining -= step.duration_seconds
        backlog = min(persisted, 100) if 60 <= seconds < 330 else 0
        item = observation(
            observed_at=start + timedelta(seconds=seconds),
            step_name=name,
            rate=rate,
            queue_work=backlog,
            persisted=persisted,
            delivered=persisted - backlog,
        ).model_copy(
            update={
                "seconds_after_load_started": seconds,
                "worker_running_count": 8 if 60 <= seconds < 540 else 1,
                "worker_desired_count": 8 if 60 <= seconds < 540 else 1,
            }
        )
        observations.append(item)
    return ElasticityResult.model_validate(
        {
            **fixed.model_dump(round_trip=True),
            "test_run_id": ELASTIC_ID,
            "load_started_at": start,
            "request_timings": [
                item.model_copy(
                    update={"observed_at": item.observed_at + timedelta(days=2)}
                )
                for item in fixed.request_timings
            ],
            "load_ended_at": start
            + timedelta(seconds=fixed.definition.duration_seconds),
            "observations": observations,
            "reconciliation": fixed.reconciliation.model_copy(
                update={"test_run_id": ELASTIC_ID}
            ),
        }
    )


def elastic_cloudwatch(result=None):
    result = result or elastic_result()
    evidence = cloudwatch_evidence(ELASTIC_ID)
    timestamps = expected_bucket_starts(result.load_started_at, result.load_ended_at)
    series = tuple(
        item.model_copy(
            update={
                "datapoints": tuple(
                    ElasticityCloudWatchDatapoint(
                        interval_started_at=timestamp,
                        value=(8 if 1 <= index < 9 else 1)
                        if item.query_id == "worker_running_tasks"
                        else item.datapoints[
                            min(index, len(item.datapoints) - 1)
                        ].value,
                    )
                    for index, timestamp in enumerate(timestamps)
                )
            }
        )
        for item in evidence.series
    )
    return evidence.model_copy(
        update={
            "window_started_at": result.load_started_at,
            "window_ended_at": result.load_ended_at,
            "collected_at": result.load_ended_at + timedelta(minutes=5),
            "series": series,
        }
    )


def changed_series(evidence, query_id, value):
    return evidence.model_copy(
        update={
            "series": tuple(
                item.model_copy(
                    update={
                        "datapoints": tuple(
                            point.model_copy(update={"value": value})
                            for point in item.datapoints
                        )
                    }
                )
                if item.query_id == query_id
                else item
                for item in evidence.series
            )
        }
    )


def ready_session(tmp_path):
    session = transition_session(tmp_path, status="worker_autoscaling_verified")
    fixed = passing_result()
    evidence = cloudwatch_evidence()
    summary = FixedControlSummary(
        measurement=fixed,
        cloudwatch=evidence,
        qualification=evaluate_fixed_control_qualification(fixed, evidence),
    )
    transition = transition_evidence().model_copy(
        update={"reset_fixed_test_run_id": fixed.test_run_id}
    )
    for directory, filename, model in (
        ("fixed", "summary.json", summary),
        ("transition", "evidence.json", transition),
    ):
        root = session.evidence_dir / "elasticity" / directory
        root.mkdir()
        (root / filename).write_text(
            model.model_dump_json(round_trip=True), encoding="utf-8"
        )
    manifest = load_manifest(session)
    manifest["git_revision"] = (
        "d" * 40
    )  # Matches the fixed controller's offline runner.
    manifest["fixed_control"] = {"summary": "elasticity/fixed/summary.json"}
    manifest["worker_autoscaling_transition"] = {
        "evidence": "elasticity/transition/evidence.json"
    }
    write_manifest(session, manifest)
    return session


def test_elastic_qualification_requires_acquisition_and_release():
    result = elastic_result()
    assert result.measurement_complete
    assert not FixedControlResult.model_validate(
        result.model_dump(round_trip=True)
    ).measurement_complete
    qualification = evaluate_elastic_treatment(
        result, elastic_cloudwatch(), policy=policy()
    )
    assert qualification.qualified
    assert qualification.maximum_observed_workers == 8
    assert qualification.scale_out_observed
    assert qualification.returned_to_minimum_during_recovery


@mark.parametrize(
    "case,reason",
    (
        ("no-scale", "scale_out_not_observed_during_demand"),
        ("post-only-return", "worker_recovery_not_observed_during_load"),
        ("over-capacity", "worker_capacity_out_of_bounds"),
        ("zero-capacity", "worker_capacity_out_of_bounds"),
        ("gap", "observation_coverage_incomplete"),
        ("dlq", "observed_dead_letter_queue_not_empty"),
        ("outstanding", "backlog_bound_exceeded"),
        ("missing-pool", "database_pool_evidence_incomplete"),
        ("undrained", "measurement_incomplete"),
        ("dropped", "measurement_incomplete"),
        ("incorrect", "measurement_incomplete"),
    ),
)
def test_observed_failures_reject_treatment(case, reason):
    result = elastic_result()
    updates = {}
    if case in {"undrained", "dropped", "incorrect"}:
        updates = {
            "undrained": {"drain_stability_confirmed": False},
            "dropped": {"dropped_iteration_count": 1},
            "incorrect": {
                "reconciliation": result.reconciliation.model_copy(
                    update={"duplicate_business_effects": 1}
                )
            },
        }[case]
    else:
        observations = []
        for item in result.observations:
            second = item.seconds_after_load_started
            if case == "gap" and 50 <= second < 110:
                continue
            change = {}
            if case == "no-scale":
                change = {"worker_running_count": 1, "worker_desired_count": 1}
            elif (
                case == "post-only-return"
                and 60 <= second < result.definition.duration_seconds
            ):
                change = {"worker_running_count": 8, "worker_desired_count": 8}
            elif case in {"over-capacity", "zero-capacity"} and second == 120:
                change = {"worker_running_count": 9 if case == "over-capacity" else 0}
            elif case == "dlq" and second == 120:
                change = {"dead_letter_queue_messages": 1}
            elif case == "outstanding" and second == 300:
                change = {"completed_delivery_events": 0}
            elif case == "missing-pool":
                change = {"api_runtime": None}
            observations.append(item.model_copy(update=change))
        updates["observations"] = tuple(observations)
    rejected = evaluate_elastic_treatment(
        result.model_copy(update=updates), elastic_cloudwatch(), policy=policy()
    )
    assert not rejected.qualified
    assert reason in rejected.rejection_reasons


@mark.parametrize(
    "query,value,reason",
    (
        ("worker_running_tasks", 1, "scale_out_not_observed_during_demand"),
        ("worker_running_tasks", 8, "worker_recovery_not_observed_during_load"),
        ("worker_running_tasks", 9, "worker_capacity_out_of_bounds"),
        ("source_queue_visible", 1501, "backlog_bound_exceeded"),
        ("source_queue_oldest_age", 181, "oldest_message_age_exceeded"),
        ("api_cpu", 70, "api_cpu_headroom_failed"),
        ("simulator_memory", 70, "simulator_memory_headroom_failed"),
        ("rds_cpu", 70, "rds_cpu_headroom_failed"),
        ("alb_target_5xx", 100, "native_ingestion_errors_failed"),
        ("dead_letter_queue_visible", 1, "native_dead_letter_queue_not_empty"),
    ),
)
def test_native_failures_reject_treatment(query, value, reason):
    qualification = evaluate_elastic_treatment(
        elastic_result(),
        changed_series(elastic_cloudwatch(), query, value),
        policy=policy(),
    )
    assert not qualification.qualified
    assert reason in qualification.rejection_reasons


def test_v5_native_latency_is_visible_corroboration_not_a_phase_gate():
    result = elastic_result()
    evidence = changed_series(elastic_cloudwatch(), "alb_p95_latency", 0.505873)
    qualification = evaluate_treatment_guardrails(result, evidence)
    assert "native_ingestion_latency_failed" not in qualification.rejection_reasons
    assert qualification.maximum_native_p95_latency_ms == 505.873
    assert evaluate_elastic_treatment(result, evidence, policy=policy()).qualified
    # Historical definitions still use the native threshold. This deliberately
    # incomplete old-shaped fixture checks the gate without reclassifying data.
    legacy = result.model_copy(update={"definition": ElasticityWorkloadDefinition()})
    assert (
        "native_ingestion_latency_failed"
        in evaluate_treatment_guardrails(legacy, evidence).rejection_reasons
    )


def test_v5_requires_a_full_minute_expanded_during_peak():
    result = elastic_result()
    observations = tuple(
        item.model_copy(update={"worker_running_count": 1, "worker_desired_count": 1})
        if item.step_name == "peak-25" and item.seconds_after_load_started < 230
        else item
        for item in result.observations
    )
    qualification = evaluate_elastic_treatment(
        result.model_copy(update={"observations": observations}),
        elastic_cloudwatch(),
        policy=policy(),
    )
    assert "expanded_peak_window_too_short" in qualification.rejection_reasons


def test_fractional_minute_launch_uses_last_complete_recovery_bucket():
    result = elastic_result()
    offset = timedelta(milliseconds=500)
    result = result.model_copy(
        update={
            "load_started_at": result.load_started_at + offset,
            "request_timings": tuple(
                item.model_copy(update={"observed_at": item.observed_at + offset})
                for item in result.request_timings
            ),
            "load_ended_at": result.load_ended_at + offset,
            "observations": tuple(
                item.model_copy(update={"observed_at": item.observed_at + offset})
                for item in result.observations
            ),
        }
    )
    assert evaluate_elastic_treatment(
        result, elastic_cloudwatch(result), policy=policy()
    ).qualified


def test_misaligned_native_window_is_not_qualified():
    with raises(ValueError, match="misaligned"):
        evaluate_elastic_treatment(
            elastic_result(), cloudwatch_evidence(ELASTIC_ID), policy=policy()
        )


def test_summary_round_trip_recomputes_qualification():
    result = elastic_result()
    native = elastic_cloudwatch()
    summary = ElasticTreatmentSummary(
        fixed_test_run_id=passing_result().test_run_id,
        policy=policy(),
        contract=ELASTIC_TREATMENT_CONTRACT,
        measurement=result,
        cloudwatch=native,
        qualification=evaluate_elastic_treatment(result, native, policy=policy()),
    )
    saved = summary.model_dump_json(round_trip=True)
    assert ElasticTreatmentSummary.model_validate_json(saved) == summary
    changed = loads(saved)
    changed["qualification"]["maximum_observed_workers"] = 7
    with raises(ValueError, match="differs from its evidence"):
        ElasticTreatmentSummary.model_validate(changed)


def test_fixed_controller_output_can_be_read_by_real_reset_loader(tmp_path):
    session = ready_session(tmp_path)
    # Run into a separate fixed evidence directory, without stubbing the loader.
    original = load_manifest(session)
    session = replace(session, evidence_root=tmp_path / "round-trip")
    session.evidence_dir.mkdir(parents=True)
    original["status"] = "async_deployed"
    write_manifest(session, original)
    run_fixed_control_session(
        session,
        **approvals(session),
        treatment_runner=lambda *a, **k: passing_result(),
        metric_collector=lambda *a, **k: cloudwatch_evidence(),
        runner=controller_runner,
    )
    _, summary = validate_experiment_reset_approval(session, **approvals(session))
    assert summary.qualification.qualified


@mark.parametrize(
    "field,value",
    (
        ("approved_session_id", "wrong"),
        ("approved_cost_ceiling_usd", "6"),
        ("approved_unconditional_teardown_session_id", "wrong"),
    ),
)
def test_invalid_approval_never_starts_or_destroys(tmp_path, field, value):
    session = ready_session(tmp_path)
    actions = []
    with raises(AwsSessionError):
        run_elastic_treatment_session(
            session,
            **{**approvals(session), field: value},
            destroyer=lambda *a: actions.append("destroy"),
            teardown_verifier=lambda *a: actions.append("verify"),
        )
    assert not actions


def test_verified_transition_is_required(tmp_path):
    session = ready_session(tmp_path)
    manifest = load_manifest(session)
    manifest["status"] = "experiment_reset_verified"
    write_manifest(session, manifest)
    with raises(AwsSessionError, match="verified worker autoscaling"):
        validate_elastic_treatment_approval(session, **approvals(session))


@mark.parametrize(
    "failure",
    (None, "reject", "workload", "metrics", "before", "after", "interrupt", "evidence"),
)
def test_controller_secures_results_and_always_tears_down(tmp_path, failure):
    session = ready_session(tmp_path)
    actions = []
    fixed_bytes = (session.evidence_dir / "elasticity/fixed/summary.json").read_bytes()
    if failure == "evidence":
        (session.evidence_dir / "elasticity/transition/evidence.json").write_text(
            "{}", encoding="utf-8"
        )

    def environment(*args, phase, **kwargs):
        actions.append(phase)
        if phase == failure:
            raise RuntimeError("environment failure")

    def treatment(*args, **kwargs):
        actions.append("workload")
        assert kwargs["treatment"] is ElasticityTreatment.ELASTIC
        assert kwargs["definition"] == passing_result().definition
        if failure == "workload":
            raise RuntimeError("driver failure")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        result = elastic_result()
        if failure == "reject":
            result = result.model_copy(update={"drain_stability_confirmed": False})
        return result

    def metrics(*args, **kwargs):
        actions.append("metrics")
        if failure == "metrics":
            raise RuntimeError("native metric failure")
        return elastic_cloudwatch()

    def destroy(*args):
        actions.append("destroy")
        if failure is None or failure in {"reject", "after"}:
            assert (session.evidence_dir / "elasticity/elastic/summary.json").is_file()
        assert (
            session.evidence_dir / "elasticity/fixed/summary.json"
        ).read_bytes() == fixed_bytes

    arguments = {
        **approvals(session),
        "treatment_runner": treatment,
        "metric_collector": metrics,
        "environment_verifier": environment,
        "runner": controller_runner,
        "destroyer": destroy,
        "teardown_verifier": lambda *a: actions.append("verify"),
    }
    if failure:
        with raises((RuntimeError, KeyboardInterrupt)):
            run_elastic_treatment_session(session, **arguments)
    else:
        summary = run_elastic_treatment_session(session, **arguments)
        assert summary.qualification.qualified
        assert load_manifest(session)["elastic_treatment"]["qualified"]
        assert actions == [
            "before",
            "workload",
            "metrics",
            "after",
            "destroy",
            "verify",
        ]
    assert actions[-2:] == ["destroy", "verify"]


@mark.parametrize("workflow_failure", (False, True))
def test_cleanup_failures_preserve_original_error_and_still_verify(
    tmp_path, workflow_failure
):
    session = ready_session(tmp_path)
    actions = []

    def treatment(*args, **kwargs):
        if workflow_failure:
            raise KeyboardInterrupt("original")
        return elastic_result()

    def destroy(*args):
        actions.append("destroy")
        raise RuntimeError("destroy failed")

    with raises(AwsElasticTreatmentCleanupError) as error:
        run_elastic_treatment_session(
            session,
            **approvals(session),
            treatment_runner=treatment,
            metric_collector=lambda *a, **k: elastic_cloudwatch(),
            environment_verifier=lambda *a, **k: None,
            runner=controller_runner,
            destroyer=destroy,
            teardown_verifier=lambda *a: actions.append("verify"),
        )
    assert actions == ["destroy", "verify"]
    assert bool(error.value.workflow_error) == workflow_failure
    assert len(error.value.cleanup_errors) == 1


@mark.parametrize("failure", (None, "plan", "capacity", "dirty-state", "policy"))
def test_live_environment_checks_are_read_only_and_fail_closed(tmp_path, failure):
    session = ready_session(tmp_path)
    root = session.evidence_dir / "elasticity/elastic"
    root.mkdir()
    calls = []

    def runner(arguments, stdin):
        call = tuple(arguments)
        calls.append(call)
        if "plan" in call:
            if failure == "plan":
                return completed(call, returncode=2)
            Path(
                next(
                    arg.removeprefix("-out=") for arg in call if arg.startswith("-out=")
                )
            ).write_bytes(b"saved plan")
            return completed(call)
        if "worker_autoscaling_policy" in call:
            return completed(call, stdout=policy().model_dump_json())
        if "describe-services" in call:
            service = call[call.index("--services") + 1]
            count = 2 if service.endswith("-api") else 1
            return completed(
                call, stdout=dumps([count, 0 if failure == "capacity" else count, 0])
            )
        if "get-queue-attributes" in call:
            return completed(
                call,
                stdout=dumps(
                    {
                        "ApproximateNumberOfMessages": "0",
                        "ApproximateNumberOfMessagesNotVisible": "0",
                        "ApproximateNumberOfMessagesDelayed": "0",
                    }
                ),
            )
        return controller_runner(call, stdin)

    def handler(request):
        state = empty_state().model_dump(mode="json")
        if failure == "dirty-state":
            state["simulator_receipts"] = 1
        return httpx.Response(200, json=state)

    native = native_verification(policy())
    if failure == "policy":
        native = native.model_copy(update={"suspended": True})
    with httpx.Client(
        base_url=API_URL, transport=httpx.MockTransport(handler)
    ) as client:
        arguments = {
            "manifest": load_manifest(session),
            "policy": policy(),
            "api_url": API_URL,
            "source_queue_url": SOURCE_QUEUE_URL,
            "dead_letter_queue_url": DLQ_URL,
            "evidence_root": root,
            "phase": "before",
            "runner": runner,
            "client": client,
            "policy_verifier": lambda *a, **k: native,
        }
        if failure:
            with raises(AwsElasticTreatmentError):
                verify_elastic_environment(session, **arguments)
        else:
            verify_elastic_environment(session, **arguments)
            assert (root / "before-plan.sha256").is_file()
            assert (root / "pre-load.json").is_file()
    assert not any("apply" in call or "update-service" in call for call in calls)


@mark.parametrize("failure", ("interrupt", "timeout"))
def test_shared_driver_stops_local_process_on_interrupt_or_timeout(
    tmp_path, monkeypatch: MonkeyPatch, failure
):
    session = ready_session(tmp_path)
    root = tmp_path / "driver"
    root.mkdir()
    actions = []

    class StuckDriver:
        def __init__(self, *args, **kwargs):
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            actions.append("terminate")

        def kill(self):
            actions.append("kill")
            self.killed = True

        def wait(self, timeout=None):
            if not self.killed:
                raise TimeoutExpired("driver", timeout)
            return 0

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("trackrelay.aws_fixed_control.Popen", StuckDriver)
    monkeypatch.setattr(
        "trackrelay.aws_fixed_control._attempt_observation", interrupted
    )
    start = elastic_result().load_started_at
    times = iter(
        (
            start,
            start
            + timedelta(
                seconds=(
                    ELASTICITY_WORKLOAD_DEFINITION.duration_seconds + 121
                    if failure == "timeout"
                    else 0
                )
            ),
        )
    )
    with (
        httpx.Client(
            base_url=API_URL,
            transport=httpx.MockTransport(lambda request: httpx.Response(201)),
        ) as client,
        raises(AwsFixedControlError if failure == "timeout" else KeyboardInterrupt),
    ):
        execute_elasticity_workload(
            session,
            treatment=ElasticityTreatment.ELASTIC,
            api_url=API_URL,
            source_queue_url=SOURCE_QUEUE_URL,
            dead_letter_queue_url=DLQ_URL,
            cluster_name="cluster",
            worker_service_name="worker",
            evidence_root=root,
            now=lambda: next(times),
            api_client=client,
        )
    assert actions == ["terminate", "kill"]
