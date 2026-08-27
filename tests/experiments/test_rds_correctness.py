"""Tests for the private four-scenario RDS correctness helper."""

from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from pytest import MonkeyPatch, raises

from trackrelay.experiments.rds_correctness import (
    CORE_CORRECTNESS_SCENARIOS,
    RDS_CORRECTNESS_EVIDENCE_PREFIX,
    RdsCorrectnessDefinition,
    encoded_rds_correctness_evidence,
    execute_rds_correctness_suite,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.scenarios import (
    CorrectnessScenario,
    ScenarioObservations,
    ScenarioRequestObservation,
    ScenarioRunArtifacts,
)

SUITE_ID = UUID("00000000-0000-0000-0000-000000000921")


def test_definition_requires_the_complete_ordered_core_suite() -> None:
    with raises(ValidationError, match="every core scenario once"):
        RdsCorrectnessDefinition(
            suite_id=SUITE_ID,
            scenarios=(CorrectnessScenario.NORMAL,),
        )


def test_suite_reuses_shared_scenarios_and_returns_only_compact_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    definition = RdsCorrectnessDefinition(suite_id=SUITE_ID)
    calls: list[CorrectnessScenario] = []

    def fake_execute(scenario: CorrectnessScenario, **kwargs):
        calls.append(scenario)
        test_run_id = kwargs["test_run_id"]
        run_directory = tmp_path / scenario.value
        run_directory.mkdir()
        observations_path = run_directory / "request-observations.json"
        observations = ScenarioObservations(
            scenario_name=scenario,
            test_run_id=test_run_id,
            requests=(
                ScenarioRequestObservation(
                    sequence_number=1,
                    partner_id=definition.partner_id,
                    partner_event_id=f"event-{scenario.value}",
                    http_status_code=(
                        502
                        if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE
                        else 201
                    ),
                    http_response_body={},
                ),
            ),
        )
        observations_path.write_text(
            observations.model_dump_json(),
            encoding="utf-8",
        )
        reconciliation = ReconciliationReport(
            test_run_id=test_run_id,
            generated=1,
            accepted=1,
            rejected=0,
            unique=1,
            processed=1,
            failed=0,
            pending=0,
            unaccounted=0,
        )
        return ScenarioRunArtifacts(
            run_directory=run_directory,
            manifest_path=run_directory / "input-manifest.json",
            observations_path=observations_path,
            summary_path=run_directory / "database-summary.json",
            reconciliation_path=run_directory / "reconciliation.json",
            reconciliation=reconciliation,
        )

    monkeypatch.setattr(
        "trackrelay.experiments.rds_correctness.execute_correctness_scenario",
        fake_execute,
    )
    evidence = execute_rds_correctness_suite(
        definition,
        output_root=tmp_path,
        trackrelay_client=object(),
        downstream_client=object(),
        downstream_url="http://downstream:8001",
    )

    assert tuple(calls) == CORE_CORRECTNESS_SCENARIOS
    assert tuple(result.scenario for result in evidence.scenario_results) == (
        CORE_CORRECTNESS_SCENARIOS
    )
    assert all(
        result.reconciliation.invariants_passed
        for result in evidence.scenario_results
    )
    encoded = encoded_rds_correctness_evidence(evidence)
    assert encoded.startswith(RDS_CORRECTNESS_EVIDENCE_PREFIX)
    assert "http://" not in encoded
