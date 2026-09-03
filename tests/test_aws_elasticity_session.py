"""Offline end-to-end controller rehearsal: real handoffs, simulated external work."""

from datetime import datetime
from functools import partial
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from signal import SIGTERM
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

from pytest import mark, raises

from tests.test_aws_elasticity_report import (
    comparison_summaries,
    fake_plotter,
    saved_session,
)
from tests.test_aws_fixed_control import controller_runner
from trackrelay.aws_async_deployment import deploy_async_stack
from trackrelay.aws_elastic_treatment import run_elastic_treatment_session
from trackrelay.aws_elasticity_report import generate_elasticity_report
from trackrelay.aws_elasticity_session import (
    ElasticitySessionError,
    _terminate_after_cleanup,
    main,
    run_elasticity_session,
)
from trackrelay.aws_elasticity_transition import (
    ElasticityTransitionEvidence,
    run_elasticity_transition_session,
)
from trackrelay.aws_experiment_reset import (
    ExperimentResetResult,
    run_experiment_reset_session,
)
from trackrelay.aws_fixed_control import run_fixed_control_session
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    destroy_session,
    load_manifest,
    plan_session,
    verify_destroyed,
    write_manifest,
)


class Rehearsal:
    """Exercise actual lifecycle/controllers without running AWS, Terraform, or k6."""

    def __init__(
        self, tmp_path: Path, *, failure=None, interrupt=False, cleanup_failure=None
    ):
        self.seed = saved_session(tmp_path / "synthetic-inputs")
        self.seed_manifest = loads((self.seed / "session.json").read_text())
        self.fixed, self.elastic = comparison_summaries()
        self.failure = failure
        self.interrupt = interrupt
        self.cleanup_failure = cleanup_failure
        self.actions = []
        self.session = AwsSession(
            session_id=self.seed_manifest["session_id"],
            profile="offline-rehearsal",
            region="ap-southeast-3",
            api_ingress_cidr="203.0.113.10/32",
            terraform_dir=tmp_path / "terraform",
            evidence_root=tmp_path / "live-evidence",
            deployment_mode="async",
        )
        plan_session(self.session, runner=self.command_runner)
        self.actions.clear()

    def action(self, name):
        self.actions.append(name)
        if name == self.failure:
            if self.interrupt:
                raise KeyboardInterrupt(f"synthetic {name} interrupt")
            raise RuntimeError(f"synthetic {name} failure")

    def command_runner(self, arguments):
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return CompletedProcess(call, 0, "", "")
        if call == ("git", "rev-parse", "HEAD"):
            return CompletedProcess(call, 0, "d" * 40, "")
        if "plan" in call:
            if "-destroy" in call:
                self.action("destroy-plan")
            else:
                self.action("plan")
            output = Path(
                next(
                    arg.removeprefix("-out=") for arg in call if arg.startswith("-out=")
                )
            )
            output.write_bytes(b"synthetic plan")
            return CompletedProcess(call, 0, "", "")
        if "apply" in call:
            name = (
                "destroy"
                if call[-1].endswith("terraform-destroy.tfplan")
                else "foundation"
            )
            self.action(name)
            if name == "destroy" and self.cleanup_failure in {"destroy", "both"}:
                return CompletedProcess(call, 1, "", "synthetic destroy failure")
            return CompletedProcess(call, 0, "", "")
        if "state" in call and "list" in call:
            return CompletedProcess(call, 0, "", "")
        if "get-resources" in call:
            return CompletedProcess(call, 0, dumps({"ResourceTagMappingList": []}), "")
        raise AssertionError(
            f"unexpected external command in offline rehearsal: {call}"
        )

    def publisher(self, session):
        self.action("publish")
        digests = {
            role: f"sha256:{letter * 64}"
            for role, letter in (("api", "a"), ("worker", "b"), ("simulator", "c"))
        }
        manifest = load_manifest(session)
        manifest["async_images"] = {
            role: {"digest": digest} for role, digest in digests.items()
        }
        write_manifest(session, manifest)
        return digests

    def phase_applier(self, session, *, phase, image_digests, services_enabled):
        self.action(phase)
        assert services_enabled is (phase == "services")

    def converged(self, session):
        self.action("converge")
        manifest = load_manifest(session)
        manifest["status"] = "async_deployed"
        write_manifest(session, manifest)

    def measurement(self, session, *, evidence_root, **kwargs):
        name = "elastic" if "treatment" in kwargs else "fixed"
        self.action(name)
        result = (
            self.elastic.measurement if name == "elastic" else self.fixed.measurement
        )
        if self.failure == f"{name}-reject":
            result = result.model_copy(update={"drain_stability_confirmed": False})
        (evidence_root / "result.json").write_text(
            result.model_dump_json(round_trip=True)
        )
        return result

    def metrics(self, session, *, test_run_id, **kwargs):
        name = (
            "fixed" if test_run_id == self.fixed.measurement.test_run_id else "elastic"
        )
        self.action(f"{name}-metrics")
        return self.fixed.cloudwatch if name == "fixed" else self.elastic.cloudwatch

    def reset(self, session, *, fixed_summary, evidence_root, **kwargs):
        self.action("reset")
        assert (
            fixed_summary.measurement.test_run_id == self.fixed.measurement.test_run_id
        )
        result = ExperimentResetResult.model_validate_json(
            (self.seed / "elasticity/reset/result.json").read_text()
        )
        (evidence_root / "result.json").write_text(result.model_dump_json())
        return result

    def transition(self, session, *, reset_result, **kwargs):
        self.action("transition")
        assert reset_result.fixed_test_run_id == self.fixed.measurement.test_run_id
        evidence = ElasticityTransitionEvidence.model_validate_json(
            (self.seed / "elasticity/transition/evidence.json").read_text()
        )
        root = session.evidence_dir / "elasticity/transition"
        root.mkdir()
        (root / "evidence.json").write_text(evidence.model_dump_json())
        manifest = load_manifest(session)
        manifest["worker_autoscaling_transition"] = {
            "git_revision": manifest["git_revision"]
        }
        write_manifest(session, manifest)
        return evidence

    def environment(self, session, *, phase, evidence_root, **kwargs):
        self.action(f"environment-{phase}")
        filenames = [
            f"{phase}-environment.json",
            f"{phase}-unchanged.tfplan",
            f"{phase}-plan.sha256",
            f"{phase}-plan.log",
        ]
        if phase == "before":
            filenames.append("pre-load.json")
        for filename in filenames:
            (evidence_root / filename).write_bytes(
                (self.seed / "elasticity/elastic" / filename).read_bytes()
            )

    def inventory(self, **kwargs):
        self.action("verify")
        counts = loads(
            (self.seed / "aws-native-inventory-after-destroy.json").read_text()
        )
        if self.cleanup_failure in {"verify", "both"}:
            counts["ecs_tasks"] = 1
        return counts

    def report(self, root):
        self.action("report")
        assert load_manifest(self.session)["status"] == "teardown_verified"
        return generate_elasticity_report(root, plotter=fake_plotter)

    def verify(self, session):
        # Keep the real verifier, but use the same simulated clock as the evidence.
        with patch("trackrelay.aws_session.datetime") as clock:
            clock.now.return_value = datetime.fromisoformat(
                self.seed_manifest["teardown_verified_at"]
            )
            verify_destroyed(
                session, runner=self.command_runner, native_inventory=self.inventory
            )

    def run(self, **overrides):
        arguments = {
            "approved_session_id": self.session.session_id,
            "approved_cost_ceiling_usd": "5.00",
            "monthly_budget_usd": "25",
            "approved_unconditional_teardown_session_id": self.session.session_id,
            "runner": self.command_runner,
            "deployer": partial(
                deploy_async_stack,
                publisher=self.publisher,
                phase_applier=self.phase_applier,
                migrator=lambda session: self.action("migration"),
                convergence_waiter=self.converged,
                migration_task_stopper=lambda session: self.action("stop-migration"),
            ),
            "fixed_runner": partial(
                run_fixed_control_session,
                treatment_runner=self.measurement,
                metric_collector=self.metrics,
                runner=controller_runner,
            ),
            "reset_runner": partial(
                run_experiment_reset_session,
                reset_runner=self.reset,
                runner=controller_runner,
            ),
            "transition_runner": partial(
                run_elasticity_transition_session, transition_runner=self.transition
            ),
            "elastic_runner": partial(
                run_elastic_treatment_session,
                treatment_runner=self.measurement,
                metric_collector=self.metrics,
                environment_verifier=self.environment,
                runner=controller_runner,
            ),
            "destroyer": partial(destroy_session, runner=self.command_runner),
            "teardown_verifier": self.verify,
            "reporter": self.report,
        }
        return run_elasticity_session(self.session, **{**arguments, **overrides})


def test_real_controller_handoffs_reach_report_after_single_teardown(
    tmp_path, monkeypatch
):
    rehearsal = Rehearsal(tmp_path)

    def prohibited(*args, **kwargs):
        raise AssertionError("offline rehearsal attempted to launch a real process")

    monkeypatch.setattr("subprocess.run", prohibited)
    monkeypatch.setattr("subprocess.Popen", prohibited)
    report = rehearsal.run()
    assert report.elasticity_demonstrated
    assert rehearsal.actions == [
        "foundation",
        "publish",
        "runtime",
        "migration",
        "services",
        "converge",
        "fixed",
        "fixed-metrics",
        "reset",
        "transition",
        "environment-before",
        "elastic",
        "elastic-metrics",
        "environment-after",
        "destroy-plan",
        "destroy",
        "verify",
        "report",
    ]
    manifest = load_manifest(rehearsal.session)
    assert manifest["status"] == "teardown_verified"
    assert manifest["elasticity_session"]["phase"] == "cloud_complete"
    assert (
        report.source_sha256["session.json"]
        == sha256(rehearsal.session.manifest_path.read_bytes()).hexdigest()
    )


@mark.parametrize(
    "failure",
    (
        "foundation",
        "publish",
        "runtime",
        "migration",
        "services",
        "converge",
        "fixed",
        "fixed-metrics",
        "reset",
        "transition",
        "environment-before",
        "elastic",
        "elastic-metrics",
        "environment-after",
    ),
)
@mark.parametrize("interrupt", (False, True))
def test_any_cloud_failure_or_interrupt_stops_and_tears_down_once(
    tmp_path, failure, interrupt
):
    rehearsal = Rehearsal(tmp_path, failure=failure, interrupt=interrupt)
    with raises(ElasticitySessionError) as caught:
        rehearsal.run()
    assert caught.value.workflow_error is not None
    assert not caught.value.cleanup_errors
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert rehearsal.actions[-1] == "verify"
    assert load_manifest(rehearsal.session)["status"] == "teardown_verified"
    assert "report" not in rehearsal.actions


@mark.parametrize("failure", ("fixed-reject", "elastic-reject"))
def test_rejected_treatments_are_not_promoted_and_negative_report_is_retained(
    tmp_path, failure
):
    rehearsal = Rehearsal(tmp_path, failure=failure)
    with raises(ElasticitySessionError):
        rehearsal.run()
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    if failure == "fixed-reject":
        assert "reset" not in rehearsal.actions
        assert "report" not in rehearsal.actions
    else:
        report = loads(
            (
                rehearsal.session.evidence_dir
                / "elasticity/report/comparison-report.json"
            ).read_text()
        )
        assert not report["elasticity_demonstrated"]
        assert report["observed_step_rate_multiplier"] is None


@mark.parametrize("failure", ("destroy", "verify", "both"))
def test_cleanup_failures_are_retained_and_never_allow_reporting(tmp_path, failure):
    rehearsal = Rehearsal(tmp_path, cleanup_failure=failure)
    with raises(ElasticitySessionError) as caught:
        rehearsal.run()
    assert len(caught.value.cleanup_errors) == (2 if failure == "both" else 1)
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert "report" not in rehearsal.actions


def test_plot_failure_leaves_cloud_off_and_report_can_be_retried_offline(tmp_path):
    rehearsal = Rehearsal(tmp_path, failure="report")
    with raises(ElasticitySessionError) as caught:
        rehearsal.run()
    assert caught.value.report_error is not None
    assert caught.value.workflow_error is None
    assert load_manifest(rehearsal.session)["status"] == "teardown_verified"
    report = generate_elasticity_report(
        rehearsal.session.evidence_dir, plotter=fake_plotter
    )
    assert report.elasticity_demonstrated


@mark.parametrize(
    "case", ("id", "teardown", "ceiling", "budget", "plan", "revision", "partial")
)
def test_invalid_approval_never_starts_or_destroys(tmp_path, case):
    rehearsal = Rehearsal(tmp_path)
    overrides = {}
    if case in {"id", "teardown", "ceiling", "budget"}:
        overrides = {
            "id": {"approved_session_id": "wrong"},
            "teardown": {"approved_unconditional_teardown_session_id": "wrong"},
            "ceiling": {"approved_cost_ceiling_usd": "0"},
            "budget": {"monthly_budget_usd": "4"},
        }[case]
    elif case == "plan":
        rehearsal.session.plan_path.write_bytes(b"changed")
    else:
        manifest = load_manifest(rehearsal.session)
        manifest["git_revision" if case == "revision" else "status"] = (
            "changed" if case == "revision" else "applied"
        )
        write_manifest(rehearsal.session, manifest)
    with raises(AwsSessionError):
        rehearsal.run(**overrides)
    assert not rehearsal.actions


def test_status_handoff_failure_cannot_skip_into_next_phase(tmp_path):
    rehearsal = Rehearsal(tmp_path)
    with raises(ElasticitySessionError) as caught:
        rehearsal.run(deployer=lambda *args, **kwargs: None)
    assert "checkpoint" in str(caught.value.workflow_error)
    assert "fixed" not in rehearsal.actions
    assert rehearsal.actions == ["foundation", "destroy-plan", "destroy", "verify"]


def test_journal_write_failure_still_attempts_teardown(tmp_path, monkeypatch):
    rehearsal = Rehearsal(tmp_path)

    def broken_journal(*args, **kwargs):
        raise OSError("synthetic journal write failure")

    monkeypatch.setattr("trackrelay.aws_elasticity_session._journal", broken_journal)
    with raises(ElasticitySessionError) as caught:
        rehearsal.run()
    assert isinstance(caught.value.workflow_error, OSError)
    assert rehearsal.actions == ["destroy-plan", "destroy", "verify"]
    assert load_manifest(rehearsal.session)["status"] == "teardown_verified"


def test_workflow_and_both_cleanup_failures_are_preserved(tmp_path):
    rehearsal = Rehearsal(tmp_path, failure="fixed", cleanup_failure="both")
    with raises(ElasticitySessionError) as caught:
        rehearsal.run()
    assert caught.value.workflow_error is not None
    assert len(caught.value.cleanup_errors) == 2
    assert rehearsal.actions.count("destroy") == 1
    assert rehearsal.actions.count("verify") == 1
    assert "reset" not in rehearsal.actions
    assert "report" not in rehearsal.actions


def test_verifier_must_record_absence_before_reporting(tmp_path):
    rehearsal = Rehearsal(tmp_path)
    with raises(ElasticitySessionError) as caught:
        rehearsal.run(teardown_verifier=lambda session: None)
    assert caught.value.cleanup_errors
    assert load_manifest(rehearsal.session)["status"] != "teardown_verified"
    assert "report" not in rehearsal.actions


@mark.parametrize("failure", (False, True))
def test_cli_passes_approvals_and_restores_sigterm_handler(
    tmp_path, monkeypatch, capsys, failure
):
    rehearsal = Rehearsal(tmp_path)
    calls = []
    previous_handler = object()
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_session.getsignal", lambda signum: previous_handler
    )
    monkeypatch.setattr(
        "trackrelay.aws_elasticity_session.signal",
        lambda signum, handler: calls.append((signum, handler)),
    )

    def simulated_session(session, **kwargs):
        assert session == rehearsal.session
        assert kwargs == {
            "approved_session_id": session.session_id,
            "approved_unconditional_teardown_session_id": session.session_id,
            "approved_cost_ceiling_usd": "5.00",
            "monthly_budget_usd": "25",
        }
        if failure:
            _terminate_after_cleanup(SIGTERM, None)
        return SimpleNamespace(conclusion="Synthetic CLI result")

    monkeypatch.setattr(
        "trackrelay.aws_elasticity_session.run_elasticity_session", simulated_session
    )
    session = rehearsal.session
    arguments = [
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
        "--approved-unconditional-teardown-session-id",
        session.session_id,
        "--approved-cost-ceiling-usd",
        "5.00",
        "--monthly-budget-usd",
        "25",
    ]
    if failure:
        with raises(SystemExit, match="AWS elasticity session failed"):
            main(arguments)
        assert not capsys.readouterr().out
    else:
        assert main(arguments) == 0
        assert "Synthetic CLI result" in capsys.readouterr().out
    assert calls == [
        (SIGTERM, _terminate_after_cleanup),
        (SIGTERM, previous_handler),
    ]
