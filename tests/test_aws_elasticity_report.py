"""Synthetic, offline comparison and publication tests; no AWS credentials used."""

from datetime import timedelta
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess

from pytest import mark, raises

from tests.test_aws_elastic_treatment import elastic_cloudwatch, elastic_result
from tests.test_aws_elasticity_transition import (
    SESSION_ID,
    native_verification,
    policy,
    reset_result,
    transition_evidence,
    transition_observation,
)
from tests.test_aws_fixed_control import cloudwatch_evidence, passing_result
from trackrelay.aws_elastic_treatment import (
    ELASTIC_TREATMENT_CONTRACT,
    ElasticTreatmentSummary,
    evaluate_elastic_treatment,
)
from trackrelay.aws_elasticity_report import (
    REQUIRED_NATIVE_INVENTORY,
    ElasticityComparisonReport,
    ElasticityReportError,
    build_comparison,
    generate_elasticity_report,
    load_comparison_evidence,
    render_markdown,
    summarize_treatment,
)
from trackrelay.aws_fixed_control import (
    FixedControlResult,
    FixedControlSummary,
    evaluate_fixed_control_qualification,
)
from trackrelay.aws_session import write_command_log


def comparison_summaries():
    """Synthetic fixed throughput 10/s versus elastic throughput 40/s when expanded."""
    original = elastic_result()
    fixed_original = passing_result()
    fixed_points, elastic_points = [], []
    fixed_completed = elastic_completed = 0
    for index, item in enumerate(original.observations):
        accepted = item.database_persisted_events
        if index:
            fixed_completed = min(accepted, fixed_completed + 200)
            capacity = 40 if 60 <= item.seconds_after_load_started < 900 else 10
            elastic_completed = min(accepted, elastic_completed + 10 * capacity)
        for completed, points, fixed in (
            (fixed_completed, fixed_points, True),
            (elastic_completed, elastic_points, False),
        ):
            points.append(
                item.model_copy(
                    update={
                        "observed_at": fixed_original.load_started_at
                        + timedelta(seconds=item.seconds_after_load_started)
                        if fixed
                        else item.observed_at,
                        "completed_delivery_events": completed,
                        "source_queue_visible_messages": accepted - completed,
                        "worker_running_count": 1
                        if fixed
                        else item.worker_running_count,
                        "worker_desired_count": 1
                        if fixed
                        else item.worker_desired_count,
                    }
                )
            )
    fixed_result = FixedControlResult.model_validate(
        {
            **fixed_original.model_dump(round_trip=True),
            "observations": fixed_points,
        }
    )
    elastic_measurement = original.model_copy(
        update={"observations": tuple(elastic_points)}
    )
    fixed_native, elastic_native = cloudwatch_evidence(), elastic_cloudwatch()
    return (
        FixedControlSummary(
            measurement=fixed_result,
            cloudwatch=fixed_native,
            qualification=evaluate_fixed_control_qualification(
                fixed_result, fixed_native
            ),
        ),
        ElasticTreatmentSummary(
            fixed_test_run_id=fixed_result.test_run_id,
            policy=policy(),
            contract=ELASTIC_TREATMENT_CONTRACT,
            measurement=elastic_measurement,
            cloudwatch=elastic_native,
            qualification=evaluate_elastic_treatment(
                elastic_measurement, elastic_native, policy=policy()
            ),
        ),
    )


def comparison(fixed=None, elastic=None):
    source, target = comparison_summaries()
    return build_comparison(
        fixed or source,
        elastic or target,
        session_id=SESSION_ID,
        region="ap-southeast-3",
        git_revision="d" * 40,
        source_sha256={"synthetic-fixture": "0" * 64},
        teardown_verified_at=target.cloudwatch.collected_at + timedelta(minutes=1),
    )


def saved_session(root: Path):
    """Write a complete synthetic evidence chain using production serialization."""
    root.mkdir(parents=True)
    fixed, elastic = comparison_summaries()
    reset = reset_result()
    count = fixed.measurement.definition.expected_request_count
    before = reset.application_reset.before.model_copy(
        update={
            "simulator_receipts": count,
            "database": reset.application_reset.before.database.model_copy(
                update={
                    "events": count,
                    "shipments": count,
                    "delivery_outbox_entries": count,
                    "delivery_attempts": count,
                }
            ),
        }
    )
    application = reset.application_reset.model_copy(
        update={
            "test_run_id": fixed.measurement.test_run_id,
            "expected_event_count": count,
            "before": before,
            "simulator_receipts_removed": count,
        }
    )
    reset = reset.model_copy(
        update={
            "fixed_test_run_id": fixed.measurement.test_run_id,
            "application_reset": application,
        }
    )
    transition = transition_evidence().model_copy(
        update={
            "reset_fixed_test_run_id": fixed.measurement.test_run_id,
            "applied_at": reset.completed_at + timedelta(seconds=1),
        }
    )
    for relative, model in (
        ("elasticity/fixed/summary.json", fixed),
        ("elasticity/elastic/summary.json", elastic),
        ("elasticity/reset/result.json", reset),
        ("elasticity/transition/evidence.json", transition),
        ("elasticity/elastic/pre-load.json", transition_observation()),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.model_dump_json(round_trip=True), encoding="utf-8")
    manifest = {
        "session_id": SESSION_ID,
        "status": "teardown_verified",
        "deployment_mode": "async",
        "git_revision": "d" * 40,
        "region": "ap-southeast-3",
        "teardown_verified_at": (
            elastic.cloudwatch.collected_at + timedelta(minutes=1)
        ).isoformat(),
        "fixed_control": {"summary": "elasticity/fixed/summary.json"},
        "elastic_treatment": {
            "summary": "elasticity/elastic/summary.json",
            "qualified": True,
        },
        "experiment_reset": {"result": "elasticity/reset/result.json"},
        "worker_autoscaling_transition": {
            "evidence": "elasticity/transition/evidence.json"
        },
    }
    for key in (
        "fixed_control",
        "elastic_treatment",
        "experiment_reset",
        "worker_autoscaling_transition",
    ):
        manifest[key]["git_revision"] = manifest["git_revision"]
    (root / "session.json").write_text(dumps(manifest), encoding="utf-8")
    (root / "aws-native-inventory-after-destroy.json").write_text(
        dumps(dict.fromkeys(REQUIRED_NATIVE_INVENTORY, 0)), encoding="utf-8"
    )
    write_command_log(
        root / "terraform-state-after-destroy.log", CompletedProcess([], 0, "", "")
    )
    for phase in ("before", "after"):
        directory = root / "elasticity/elastic"
        native = native_verification(policy())
        if phase == "after":
            native = native.model_copy(
                update={"collected_at": elastic.cloudwatch.collected_at}
            )
        (directory / f"{phase}-environment.json").write_text(
            native.model_dump_json(), encoding="utf-8"
        )
        plan = f"synthetic {phase} plan".encode()
        (directory / f"{phase}-unchanged.tfplan").write_bytes(plan)
        (directory / f"{phase}-plan.sha256").write_text(
            sha256(plan).hexdigest(), encoding="utf-8"
        )
        write_command_log(
            directory / f"{phase}-plan.log", CompletedProcess([], 0, "No changes.", "")
        )
    return root


def fake_plotter(_report, _fixed, _elastic, output):
    (output / "comparison.png").write_bytes(b"test image")
    (output / "comparison.svg").write_text("<svg/>", encoding="utf-8")


def test_comparison_requires_completion_not_only_api_latency():
    report = comparison()
    assert report.elasticity_demonstrated
    assert report.fixed.highest_supported_rate_per_second == 10
    assert report.elastic.highest_supported_rate_per_second == 25
    assert report.observed_step_rate_multiplier == 2.5
    peak = next(
        item for item in report.fixed.step_results if item.step_name == "peak-25"
    )
    assert peak.completed_events_per_second == 20
    assert peak.outstanding_change == 1500
    assert "completion_rate_below_offered_rate" in peak.rejection_reasons
    assert report.elastic.scale_out_seconds_after_start == 60
    assert report.elastic.return_to_minimum_seconds_after_start == 900
    assert report.elastic.stable_drain_seconds_after_load == 0
    assert report.elastic.drain_confirmed_seconds_after_load == 180
    assert (
        ElasticityComparisonReport.model_validate_json(report.model_dump_json())
        == report
    )
    markdown = render_markdown(report)
    assert "not maximum production capacity" in markdown
    assert "comparison.png" in markdown


def test_sparse_fixed_observations_cannot_establish_rate_multiplier():
    fixed, elastic = comparison_summaries()
    sparse = fixed.measurement.model_copy(
        update={
            "observations": (
                fixed.measurement.observations[0],
                *fixed.measurement.observations[3::6],
            )
        }
    )
    fixed = fixed.model_copy(update={"measurement": sparse})
    report = comparison(fixed, elastic)
    assert not report.elasticity_demonstrated
    assert report.fixed.highest_supported_rate_per_second is None
    assert report.observed_step_rate_multiplier is None
    assert "not established" in report.conclusion


def test_stable_drain_flag_without_180_seconds_of_samples_is_insufficient():
    fixed, _ = comparison_summaries()
    result = fixed.measurement.model_copy(
        update={"observations": fixed.measurement.observations[:-5]}
    )
    report = summarize_treatment(fixed.model_copy(update={"measurement": result}))
    assert report.drain_confirmed_seconds_after_load is None
    assert report.highest_supported_rate_per_second is None
    assert "stable_drain_not_established" in report.measurement_rejection_reasons


def test_all_occurrences_of_rate_must_pass():
    fixed, _ = comparison_summaries()
    points = tuple(
        item.model_copy(update={"source_queue_visible_messages": 1501})
        if item.seconds_after_load_started == 630
        else item
        for item in fixed.measurement.observations
    )
    result = summarize_treatment(
        fixed.model_copy(
            update={
                "measurement": fixed.measurement.model_copy(
                    update={"observations": points}
                )
            }
        )
    )
    assert next(
        item for item in result.step_results if item.step_name == "rise-10"
    ).supported
    assert not next(
        item for item in result.step_results if item.step_name == "fall-10"
    ).supported
    assert result.highest_supported_rate_per_second == 5


@mark.parametrize(
    "case",
    ("elapsed", "decreasing", "overdelivered", "definition", "run-id", "qualification"),
)
def test_inconsistent_evidence_is_rejected(case):
    fixed, elastic = comparison_summaries()
    if case == "definition":
        elastic = elastic.model_copy(
            update={
                "measurement": elastic.measurement.model_copy(
                    update={
                        "definition": elastic.measurement.definition.model_copy(
                            update={"random_seed": 42}
                        )
                    }
                )
            }
        )
    elif case == "run-id":
        elastic = elastic.model_copy(
            update={"fixed_test_run_id": elastic.measurement.test_run_id}
        )
    elif case == "qualification":
        fixed = fixed.model_copy(
            update={
                "qualification": fixed.qualification.model_copy(
                    update={"qualified": False}
                )
            }
        )
    else:
        observations = list(fixed.measurement.observations)
        changed = {
            "elapsed": {"seconds_after_load_started": 99},
            "decreasing": {"completed_delivery_events": 0},
            "overdelivered": {"completed_delivery_events": 9999},
        }[case]
        observations[10] = observations[10].model_copy(update=changed)
        fixed = fixed.model_copy(
            update={
                "measurement": fixed.measurement.model_copy(
                    update={"observations": tuple(observations)}
                )
            }
        )
    with raises(ElasticityReportError):
        comparison(fixed, elastic)


def test_rejected_elastic_evidence_produces_negative_report():
    fixed, elastic = comparison_summaries()
    points = tuple(
        item.model_copy(update={"worker_running_count": 8, "worker_desired_count": 8})
        if 720 <= item.seconds_after_load_started < 1140
        else item
        for item in elastic.measurement.observations
    )
    measurement = elastic.measurement.model_copy(update={"observations": points})
    elastic = elastic.model_copy(
        update={
            "measurement": measurement,
            "qualification": evaluate_elastic_treatment(
                measurement, elastic.cloudwatch, policy=elastic.policy
            ),
        }
    )
    report = comparison(fixed, elastic)
    assert not report.elasticity_demonstrated
    assert report.observed_step_rate_multiplier is None
    assert (
        "worker_recovery_not_observed_during_load" in report.elastic.rejection_reasons
    )


def test_missing_elastic_observations_are_unavailable_not_zero_capacity():
    fixed, elastic = comparison_summaries()
    measurement = elastic.measurement.model_copy(update={"observations": ()})
    elastic = elastic.model_copy(
        update={
            "measurement": measurement,
            "qualification": evaluate_elastic_treatment(
                measurement, elastic.cloudwatch, policy=elastic.policy
            ),
        }
    )
    report = comparison(fixed, elastic)
    assert not report.elasticity_demonstrated
    assert report.elastic.maximum_workers is None
    assert report.elastic.highest_supported_rate_per_second is None
    assert report.observed_step_rate_multiplier is None


def test_report_model_refuses_an_unearned_multiplier():
    document = comparison().model_dump(mode="json")
    document["observed_step_rate_multiplier"] = 8
    with raises(ValueError, match="claim differs"):
        ElasticityComparisonReport.model_validate(document)


def test_generation_reads_saved_files_without_external_commands(tmp_path, monkeypatch):
    root = saved_session(tmp_path / "session")
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }

    def prohibited(*args, **kwargs):
        raise AssertionError("offline report attempted to start a subprocess")

    monkeypatch.setattr("subprocess.run", prohibited)
    monkeypatch.setattr("subprocess.Popen", prohibited)
    report = generate_elasticity_report(root, plotter=fake_plotter)
    assert report.elasticity_demonstrated
    assert set(report.source_sha256) == {str(path) for path in before}
    assert all(
        (root / path).read_bytes() == content for path, content in before.items()
    )
    assert {path.name for path in (root / "elasticity/report").iterdir()} == {
        "comparison-report.json",
        "comparison-report.md",
        "comparison.png",
        "comparison.svg",
    }
    with raises(ElasticityReportError, match="already exists"):
        generate_elasticity_report(root, plotter=fake_plotter)


@mark.parametrize(
    "failure",
    (
        "status",
        "inventory-missing",
        "inventory-positive",
        "inventory-legacy-logs",
        "inventory-insights-positive",
        "state",
        "plan-hash",
        "plan-exit",
        "policy",
        "reset",
        "journal",
        "symlink",
    ),
)
def test_loader_refuses_unverified_or_mixed_session_evidence(tmp_path, failure):
    root = saved_session(tmp_path / "session")
    path = root / "session.json"
    if failure in {"status", "journal"}:
        data = loads(path.read_text())
        if failure == "status":
            data["status"] = "elastic_treatment_qualified"
        else:
            data["elastic_treatment"]["summary"] = "../other/summary.json"
        path.write_text(dumps(data))
    elif failure.startswith("inventory"):
        data = dict.fromkeys(REQUIRED_NATIVE_INVENTORY, 0)
        if failure == "inventory-missing":
            del data["application_autoscaling_targets"]
        elif failure == "inventory-legacy-logs":
            del data["container_insights_log_groups"]
        elif failure == "inventory-insights-positive":
            data["container_insights_log_groups"] = 1
        else:
            data["ecs_tasks"] = 1
        (root / "aws-native-inventory-after-destroy.json").write_text(dumps(data))
    elif failure == "state":
        write_command_log(
            root / "terraform-state-after-destroy.log",
            CompletedProcess([], 0, "aws_ecs_service.worker", ""),
        )
    elif failure == "plan-hash":
        (root / "elasticity/elastic/after-plan.sha256").write_text("0" * 64)
    elif failure == "plan-exit":
        write_command_log(
            root / "elasticity/elastic/before-plan.log",
            CompletedProcess([], 2, "Changes", ""),
        )
    elif failure == "policy":
        data = native_verification(policy()).model_dump(mode="json")
        data["suspended"] = True
        (root / "elasticity/elastic/after-environment.json").write_text(dumps(data))
    elif failure == "reset":
        (root / "elasticity/reset/result.json").write_text("{}")
    else:
        external = tmp_path / "external.json"
        path.rename(external)
        path.symlink_to(external)
    with raises(ElasticityReportError):
        generate_elasticity_report(root, plotter=fake_plotter)
    assert not (root / "elasticity/report").exists()


@mark.parametrize("failure", ("exception", "interrupt", "missing-svg"))
def test_plot_failure_does_not_publish_partial_report(tmp_path, failure):
    root = saved_session(tmp_path / "session")

    def fail(_report, _fixed, _elastic, staging):
        (staging / "comparison.png").write_bytes(b"partial")
        if failure == "exception":
            raise RuntimeError("plot failed")
        if failure == "interrupt":
            raise KeyboardInterrupt()

    with raises(KeyboardInterrupt if failure == "interrupt" else ElasticityReportError):
        generate_elasticity_report(root, plotter=fail)
    assert not (root / "elasticity/report").exists()
    assert not list((root / "elasticity").glob(".elasticity-report-*"))


def test_real_headless_plot_outputs_png_and_svg(tmp_path):
    root = saved_session(tmp_path / "synthetic-session")
    generate_elasticity_report(root)
    png = root / "elasticity/report/comparison.png"
    svg = root / "elasticity/report/comparison.svg"
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert png.stat().st_size > 10000
    assert "<svg" in svg.read_text()
    assert "500 ms SLO" in svg.read_text()


def test_raw_no_state_file_log_is_valid_teardown(tmp_path):
    root = saved_session(tmp_path / "session")
    write_command_log(
        root / "terraform-state-after-destroy.log",
        CompletedProcess([], 1, "", "No state file was found!"),
    )
    assert load_comparison_evidence(root)[0].qualification.qualified
