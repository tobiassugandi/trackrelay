"""Offline coverage for the non-headline elastic-only diagnostic workflow."""

from functools import partial
from json import dumps, loads
from signal import SIGTERM

from pytest import mark, raises

from tests.test_aws_elastic_treatment import (
    elastic_cloudwatch,
    elastic_result,
)
from tests.test_aws_elastic_treatment import (
    ready_session as paired_ready_session,
)
from tests.test_aws_elasticity_session import Rehearsal
from tests.test_aws_elasticity_transition import transition_evidence
from tests.test_aws_fixed_control import controller_runner
from trackrelay.aws_async_deployment import deploy_async_stack
from trackrelay.aws_elasticity_diagnostic import (
    AwsElasticityDiagnosticError,
    ElasticityDiagnosticSummary,
    _terminate_after_cleanup,
    evaluate_elastic_treatment,
    main,
    run_elastic_diagnostic_treatment,
    run_elasticity_diagnostic_session,
)
from trackrelay.aws_elasticity_report import (
    ElasticityReportError,
    load_comparison_evidence,
)
from trackrelay.aws_elasticity_transition import DiagnosticTransitionEvidence
from trackrelay.aws_session import (
    AwsSessionError,
    destroy_session,
    load_manifest,
    write_manifest,
)


def diagnostic_transition():
    return DiagnosticTransitionEvidence.model_validate(
        {**transition_evidence().model_dump(), "reset_fixed_test_run_id": None}
    )


def diagnostic_summary(transition=None):
    transition = transition or diagnostic_transition()
    result = elastic_result()
    cloudwatch = elastic_cloudwatch(result)
    return ElasticityDiagnosticSummary(
        transition=transition,
        contract={"schema_version": 1},
        measurement=result,
        cloudwatch=cloudwatch,
        qualification=evaluate_elastic_treatment(
            result,
            cloudwatch,
            policy=transition.policy,
        ),
    )


def test_diagnostic_treatment_uses_distinct_evidence_and_no_fixed_lineage(
    tmp_path,
) -> None:
    session = paired_ready_session(tmp_path)
    manifest = load_manifest(session)
    for key in ("fixed_control", "experiment_reset", "elastic_treatment"):
        manifest.pop(key, None)
    manifest["status"] = "worker_autoscaling_diagnostic_verified"
    write_manifest(session, manifest)
    actions = []

    def treatment(_session, *, evidence_root, **_kwargs):
        actions.append("workload")
        result = elastic_result()
        (evidence_root / "result.json").write_text(
            result.model_dump_json(round_trip=True), encoding="utf-8"
        )
        return result

    def environment(_session, *, phase, **_kwargs):
        actions.append(f"environment-{phase}")

    summary = run_elastic_diagnostic_treatment(
        session,
        transition=diagnostic_transition(),
        treatment_runner=treatment,
        metric_collector=lambda *_args, **_kwargs: (
            actions.append("metrics") or elastic_cloudwatch()
        ),
        environment_verifier=environment,
        runner=controller_runner,
    )

    assert summary.headline_eligible is False
    assert summary.comparison_multiplier is None
    assert summary.qualification.qualified
    assert summary.transition.reset_fixed_test_run_id is None
    assert actions == [
        "environment-before",
        "workload",
        "metrics",
        "environment-after",
    ]
    assert not (session.evidence_dir / "elasticity/elastic").exists()
    assert (
        session.evidence_dir / "elasticity/diagnostic/elastic/summary.json"
    ).is_file()
    manifest = load_manifest(session)
    assert manifest["status"] == "elastic_diagnostic_qualified"
    assert manifest["elastic_diagnostic"]["headline_eligible"] is False
    assert "fixed_test_run_id" not in manifest["elastic_diagnostic"]


def test_diagnostic_summary_rejects_paired_transition_provenance() -> None:
    with raises(ValueError):
        diagnostic_summary(transition_evidence())


def run_rehearsal(rehearsal: Rehearsal, *, failure=None, **overrides):
    transition = diagnostic_transition()

    def transition_runner(session, *, reset_result, **_kwargs):
        rehearsal.action("transition")
        assert reset_result is None
        if failure == "transition":
            raise RuntimeError("synthetic diagnostic transition failure")
        root = session.evidence_dir / "elasticity/diagnostic/transition"
        root.mkdir(parents=True)
        (root / "evidence.json").write_text(transition.model_dump_json())
        return transition

    def treatment_runner(session, *, evidence_root, **_kwargs):
        rehearsal.action("diagnostic")
        result = elastic_result()
        if failure == "reject":
            result = result.model_copy(update={"drain_stability_confirmed": False})
        (evidence_root / "result.json").write_text(
            result.model_dump_json(round_trip=True)
        )
        return result

    arguments = {
        "approved_session_id": rehearsal.session.session_id,
        "approved_cost_ceiling_usd": "5.00",
        "approved_unconditional_teardown_session_id": rehearsal.session.session_id,
        "monthly_budget_usd": "25",
        "runner": rehearsal.command_runner,
        "process_runner": controller_runner,
        "deployer": partial(
            deploy_async_stack,
            publisher=rehearsal.publisher,
            phase_applier=rehearsal.phase_applier,
            migrator=lambda session: rehearsal.action("migration"),
            convergence_waiter=rehearsal.converged,
            migration_task_stopper=lambda session: rehearsal.action("stop-migration"),
        ),
        "transition_runner": transition_runner,
        "diagnostic_runner": partial(
            run_elastic_diagnostic_treatment,
            treatment_runner=treatment_runner,
            metric_collector=lambda *a, **k: (
                rehearsal.action("metrics") or elastic_cloudwatch()
            ),
            environment_verifier=lambda *a, phase, **k: rehearsal.action(
                f"environment-{phase}"
            ),
        ),
        "destroyer": partial(
            destroy_session,
            runner=rehearsal.command_runner,
            telemetry_cleaner=lambda **kwargs: None,
        ),
        "teardown_verifier": rehearsal.verify,
        "diagnostic_collector": lambda *args, **kwargs: None,
    }
    return run_elasticity_diagnostic_session(
        rehearsal.session, **{**arguments, **overrides}
    )


def test_diagnostic_session_skips_fixed_reset_and_report_then_tears_down(
    tmp_path,
) -> None:
    rehearsal = Rehearsal(tmp_path)

    summary = run_rehearsal(rehearsal)

    assert summary.qualification.qualified
    assert rehearsal.actions == [
        "foundation",
        "publish",
        "runtime",
        "migration",
        "services",
        "converge",
        "transition",
        "environment-before",
        "diagnostic",
        "metrics",
        "environment-after",
        "destroy-plan",
        "destroy",
        "verify",
    ]
    manifest = load_manifest(rehearsal.session)
    assert manifest["status"] == "teardown_verified"
    journal = manifest["elasticity_diagnostic_session"]
    assert journal["phase"] == "cloud_complete"
    assert journal["headline_eligible"] is False
    assert journal["comparison_report_allowed"] is False
    assert "fixed_control" not in manifest
    assert "experiment_reset" not in manifest
    assert "elasticity_session" not in manifest


def test_diagnostic_failure_still_destroys_and_verifies_once(tmp_path) -> None:
    rehearsal = Rehearsal(tmp_path)

    with raises(AwsElasticityDiagnosticError) as caught:
        run_rehearsal(rehearsal, failure="transition")

    assert isinstance(caught.value.workflow_error, RuntimeError)
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert "diagnostic" not in rehearsal.actions
    manifest = load_manifest(rehearsal.session)
    assert manifest["status"] == "teardown_verified"
    assert manifest["elasticity_diagnostic_session"]["phase"] == "cloud_failed"


def test_diagnostic_rejects_partial_session_before_cleanup(tmp_path) -> None:
    rehearsal = Rehearsal(tmp_path)
    manifest = load_manifest(rehearsal.session)
    manifest["elasticity_diagnostic_session"] = {"phase": "partial"}
    write_manifest(rehearsal.session, manifest)

    with raises(AwsSessionError, match="fresh reviewed plan"):
        run_rehearsal(rehearsal)

    assert rehearsal.actions == []


@mark.parametrize(
    "phase",
    (
        "foundation",
        "publish",
        "migration",
        "transition",
        "diagnostic",
        "metrics",
        "environment-before",
        "environment-after",
    ),
)
@mark.parametrize("interrupt", (False, True))
def test_each_diagnostic_phase_failure_cleans_up(tmp_path, phase, interrupt):
    rehearsal = Rehearsal(tmp_path, failure=phase, interrupt=interrupt)
    with raises(AwsElasticityDiagnosticError):
        run_rehearsal(rehearsal)
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert load_manifest(rehearsal.session)["status"] == "teardown_verified"


@mark.parametrize("failure", ("destroy", "verify", "both"))
def test_diagnostic_cleanup_failure_prevents_success(tmp_path, failure):
    rehearsal = Rehearsal(tmp_path, cleanup_failure=failure)
    with raises(AwsElasticityDiagnosticError) as caught:
        run_rehearsal(rehearsal)
    assert len(caught.value.cleanup_errors) == (2 if failure == "both" else 1)
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert (
        load_manifest(rehearsal.session)["elasticity_diagnostic_session"]["phase"]
        == "cloud_failed"
    )


def test_journal_failure_cannot_skip_diagnostic_teardown(tmp_path, monkeypatch):
    rehearsal = Rehearsal(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("synthetic journal failure")

    monkeypatch.setattr(
        "trackrelay.aws_elasticity_diagnostic._journal_diagnostic", broken
    )
    with raises(AwsElasticityDiagnosticError):
        run_rehearsal(rehearsal)
    assert rehearsal.actions == ["destroy-plan", "destroy", "verify"]


def test_report_rejects_diagnostic_even_with_complete_paired_files(tmp_path):
    rehearsal = Rehearsal(tmp_path)

    path = rehearsal.seed / "session.json"
    manifest = loads(path.read_text())
    manifest["elasticity_diagnostic_session"] = {"kind": "elastic-only-diagnostic"}
    path.write_text(dumps(manifest))
    with raises(ElasticityReportError, match="cannot produce a paired comparison"):
        load_comparison_evidence(rehearsal.seed)


def test_paired_session_rejects_diagnostic_journal_before_spending(tmp_path):
    rehearsal = Rehearsal(tmp_path)
    manifest = load_manifest(rehearsal.session)
    manifest["elasticity_diagnostic_session"] = {"phase": "partial"}
    write_manifest(rehearsal.session, manifest)
    with raises(AwsSessionError, match="fresh reviewed plan"):
        rehearsal.run()
    assert rehearsal.actions == []


def test_rejected_diagnostic_retains_negative_summary_and_cleans_up(tmp_path):
    rehearsal = Rehearsal(tmp_path)
    with raises(AwsElasticityDiagnosticError):
        run_rehearsal(rehearsal, failure="reject")
    summary = ElasticityDiagnosticSummary.model_validate_json(
        (
            rehearsal.session.evidence_dir
            / "elasticity/diagnostic/elastic/summary.json"
        ).read_text()
    )
    assert not summary.qualification.qualified
    assert "measurement_incomplete" in summary.qualification.rejection_reasons
    assert summary.comparison_multiplier is None
    assert rehearsal.actions[-3:] == ["destroy-plan", "destroy", "verify"]


@mark.parametrize("case", ("id", "teardown", "ceiling", "budget", "plan", "revision"))
def test_invalid_diagnostic_approval_never_starts_or_cleans_up(tmp_path, case):
    rehearsal = Rehearsal(tmp_path)
    overrides = {
        "id": {"approved_session_id": "wrong"},
        "teardown": {"approved_unconditional_teardown_session_id": "wrong"},
        "ceiling": {"approved_cost_ceiling_usd": "0"},
        "budget": {"monthly_budget_usd": "4"},
    }.get(case, {})
    if case == "plan":
        rehearsal.session.plan_path.write_bytes(b"changed plan")
    if case == "revision":
        manifest = load_manifest(rehearsal.session)
        manifest["git_revision"] = "changed"
        write_manifest(rehearsal.session, manifest)
    with raises(AwsSessionError):
        run_rehearsal(rehearsal, **overrides)
    assert not rehearsal.actions


def test_cli_restores_signal_handler_and_labels_output_non_comparative(
    tmp_path, monkeypatch, capsys
) -> None:
    rehearsal = Rehearsal(tmp_path)
    previous = object()
    calls = []
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_diagnostic.getsignal", lambda _signal: previous
    )
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_diagnostic.signal",
        lambda signum, handler: calls.append((signum, handler)),
    )
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_diagnostic.run_elasticity_diagnostic_session",
        lambda _session, **_kwargs: diagnostic_summary(),
    )
    session = rehearsal.session

    assert (
        main(
            [
                "--session-id",
                session.session_id,
                "--profile",
                session.profile,
                "--region",
                session.region,
                "--api-ingress-cidr",
                session.api_ingress_cidr,
                "--deployment-mode",
                "async",
                "--terraform-dir",
                str(session.terraform_dir),
                "--evidence-root",
                str(session.evidence_root),
                "--approved-session-id",
                session.session_id,
                "--approved-cost-ceiling-usd",
                "5",
                "--approved-unconditional-teardown-session-id",
                session.session_id,
                "--monthly-budget-usd",
                "25",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "no comparison multiplier was produced" in output
    assert calls == [(SIGTERM, _terminate_after_cleanup), (SIGTERM, previous)]
