"""Tests for guarded RDS correctness evidence collection."""

from base64 import b64encode
from collections.abc import Sequence
from datetime import UTC, datetime
from json import loads
from pathlib import Path
from shlex import split
from subprocess import CompletedProcess

from tests.test_aws_rehost import (
    COMMAND_ID,
    GIT_REVISION,
    INSTANCE_ID,
    completed,
    make_session,
    terraform_output_name,
)
from trackrelay.aws_rds_correctness import collect_rds_correctness
from trackrelay.experiments.rds_correctness import (
    RDS_CORRECTNESS_EVIDENCE_PREFIX,
    RdsCorrectnessDefinition,
    RdsCorrectnessEvidence,
    RdsScenarioEvidence,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.scenarios import CorrectnessScenario


def evidence_for_payload(payload_path: Path) -> RdsCorrectnessEvidence:
    payload = loads(payload_path.read_text(encoding="utf-8"))
    command = split(payload["commands"][0].splitlines()[-1])

    def value(name: str) -> str:
        return command[command.index(name) + 1]

    definition = RdsCorrectnessDefinition(
        suite_id=value("--suite-id"),
        random_seed=int(value("--seed")),
        shipment_count=int(value("--shipments")),
        partner_id=value("--partner-id"),
        start_at=value("--start-at"),
    )
    results = []
    for scenario in definition.scenarios:
        test_run_id = definition.test_run_id(scenario)
        generated = 10 if scenario is CorrectnessScenario.DUPLICATE else 5
        unique = 5
        http_status_codes = (201,) * 5
        if scenario is CorrectnessScenario.DUPLICATE:
            http_status_codes += (200,) * 5
        elif scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE:
            http_status_codes = (502,) * 5
        results.append(
            RdsScenarioEvidence(
                scenario=scenario,
                test_run_id=test_run_id,
                http_status_codes=http_status_codes,
                reconciliation=ReconciliationReport(
                    test_run_id=test_run_id,
                    generated=generated,
                    accepted=generated,
                    rejected=0,
                    unique=unique,
                    processed=unique,
                    failed=0,
                    pending=0,
                    unaccounted=0,
                    simulator_receipts=(
                        0
                        if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE
                        else unique
                    ),
                    simulator_unique_events=(
                        0
                        if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE
                        else unique
                    ),
                ),
            )
        )
    return RdsCorrectnessEvidence(
        definition=definition,
        scenario_results=tuple(results),
    )


def test_rds_correctness_runs_privately_and_saves_compact_evidence(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="rds_deployed")
    remote_evidence: RdsCorrectnessEvidence | None = None
    captured_command = ""

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        nonlocal remote_evidence, captured_command
        del input_text
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        if terraform_output_name(call) == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if "send-command" in call:
            parameter = call[call.index("--parameters") + 1]
            payload_path = Path(parameter.removeprefix("file://"))
            payload = loads(payload_path.read_text(encoding="utf-8"))
            captured_command = payload["commands"][0]
            remote_evidence = evidence_for_payload(payload_path)
            return completed(call, stdout=COMMAND_ID)
        if "get-command-invocation" in call:
            query = call[call.index("--query") + 1]
            if query == "[Status,ResponseCode]":
                return completed(call, stdout="Success\t0")
            assert remote_evidence is not None
            encoded = b64encode(
                remote_evidence.model_dump_json().encode("utf-8")
            ).decode("ascii")
            return completed(
                call,
                stdout=f"{RDS_CORRECTNESS_EVIDENCE_PREFIX}{encoded}\n",
            )
        return completed(call)

    completed_at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    evidence = collect_rds_correctness(
        session,
        runner=runner,
        now=lambda: completed_at,
    )

    assert len(evidence.scenario_results) == 4
    assert "http://api:8000" not in captured_command
    assert "http://downstream:8001" not in captured_command
    assert "POSTGRES_PASSWORD" not in captured_command
    assert "rds.amazonaws.com" not in captured_command
    saved_text = (
        session.evidence_dir / "rds-correctness" / "suite.json"
    ).read_text(encoding="utf-8")
    assert "123456789012" not in saved_text
    assert "http://" not in saved_text
    saved_manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert saved_manifest["status"] == "rds_correctness_collected"
    assert saved_manifest["rds_correctness"] == {
        "all_scenarios_passed": True,
        "completed_at": "2026-08-27T12:00:00Z",
        "result": "rds-correctness/suite.json",
        "scenario_count": 4,
        "schema_version": 1,
    }
