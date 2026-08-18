"""Run repeatable local correctness scenarios and save their evidence."""

import json
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.config import Settings
from trackrelay.database import session_factory as default_session_factory
from trackrelay.downstream.control import SimulatorMode
from trackrelay.experiments.generator import (
    DEFAULT_START_AT,
    GeneratorConfiguration,
    InputManifest,
    ManifestEvent,
    generate_input_manifest,
    write_input_manifest,
)
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    fetch_simulator_receipts,
    reconcile_manifest,
    write_reconciliation_report,
)
from trackrelay.models import Partner
from trackrelay.models import TestRun as ExperimentRunModel


class CorrectnessScenario(StrEnum):
    """The repeatable correctness behaviors exposed as local commands."""

    NORMAL = "normal"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out-of-order"
    DOWNSTREAM_OUTAGE = "downstream-outage"


class ScenarioRequestObservation(BaseModel):
    """One HTTP outcome observed while sending a manifest request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_number: int
    partner_id: str
    partner_event_id: str
    http_status_code: int
    http_response_body: JsonValue


class ScenarioObservations(BaseModel):
    """Stable request-level evidence captured for one scenario run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    scenario_name: CorrectnessScenario
    test_run_id: UUID
    requests: tuple[ScenarioRequestObservation, ...]


@dataclass(frozen=True)
class ScenarioRunArtifacts:
    """Paths and reconciliation outcome produced by one scenario run."""

    run_directory: Path
    manifest_path: Path
    observations_path: Path
    summary_path: Path
    reconciliation_path: Path
    reconciliation: ReconciliationReport


class ScenarioExecutionError(RuntimeError):
    """Raised after saving evidence for an incorrect scenario outcome."""


def _resequence_manifest_events(
    manifest_events: tuple[ManifestEvent, ...],
) -> tuple[ManifestEvent, ...]:
    return tuple(
        manifest_event.model_copy(update={"sequence_number": sequence_number})
        for sequence_number, manifest_event in enumerate(
            manifest_events,
            start=1,
        )
    )


def build_correctness_manifest(
    scenario: CorrectnessScenario,
    *,
    seed: int,
    configuration: GeneratorConfiguration,
    test_run_id: UUID,
) -> InputManifest:
    """Shape one normal history into the selected correctness scenario."""
    normal_manifest = generate_input_manifest(
        seed=seed,
        configuration=configuration,
        test_run_id=test_run_id,
    )

    if scenario is CorrectnessScenario.DUPLICATE:
        ordered_events = (
            normal_manifest.expected_events + normal_manifest.expected_events
        )
    elif scenario is CorrectnessScenario.OUT_OF_ORDER:
        events_by_tracking_number = {
            tracking_number: tuple(
                manifest_event
                for manifest_event in normal_manifest.expected_events
                if manifest_event.tracking_number == tracking_number
            )
            for tracking_number in normal_manifest.expected_final_shipments
        }
        ordered_events = tuple(
            manifest_event
            for tracking_number in normal_manifest.expected_final_shipments
            for manifest_event in reversed(
                events_by_tracking_number[tracking_number]
            )
        )
    else:
        ordered_events = normal_manifest.expected_events

    manifest_events = _resequence_manifest_events(ordered_events)
    return InputManifest(
        test_run_id=test_run_id,
        scenario_name=scenario.value,
        seed=seed,
        configuration=configuration,
        events_generated=len(manifest_events),
        expected_unique_events=normal_manifest.expected_unique_events,
        expected_events=manifest_events,
        expected_final_shipments=normal_manifest.expected_final_shipments,
    )


def _prepare_database_for_run(
    manifest: InputManifest,
    *,
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as session:
        if session.get(ExperimentRunModel, manifest.test_run_id) is not None:
            raise ValueError(
                f"Test run {manifest.test_run_id} already exists"
            )

        database_partner = session.get(
            Partner,
            manifest.configuration.partner_id,
        )
        if database_partner is None:
            session.add(
                Partner(
                    id=manifest.configuration.partner_id,
                    name=f"Experiment {manifest.configuration.partner_id}",
                    adapter_type="courier-alpha",
                    is_active=True,
                )
            )
        elif (
            database_partner.adapter_type != "courier-alpha"
            or not database_partner.is_active
        ):
            raise ValueError(
                "Scenario partner must be active and use courier-alpha"
            )

        session.add(
            ExperimentRunModel(
                id=manifest.test_run_id,
                scenario_name=manifest.scenario_name,
                random_seed=manifest.seed,
                configuration=manifest.configuration.model_dump(mode="json"),
                expected_event_count=manifest.events_generated,
            )
        )


def _complete_database_run(
    test_run_id: UUID,
    *,
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as session:
        database_test_run = session.get(ExperimentRunModel, test_run_id)
        if database_test_run is None:
            raise ValueError(f"Test run {test_run_id} disappeared")
        database_test_run.completed_at = datetime.now(UTC)


def _write_observations(
    observations: ScenarioObservations,
    output_path: Path,
) -> None:
    output_path.write_text(
        f"{observations.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )


def _write_json_response(response: httpx.Response, output_path: Path) -> None:
    output_path.write_text(
        f"{json.dumps(response.json(), indent=2)}\n",
        encoding="utf-8",
    )


def _expected_http_status_codes(
    scenario: CorrectnessScenario,
    manifest: InputManifest,
) -> tuple[int, ...]:
    if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE:
        return (502,) * manifest.events_generated

    seen_identities: set[tuple[str, str]] = set()
    http_status_codes = []
    for manifest_event in manifest.expected_events:
        identity = (
            manifest_event.partner_id,
            manifest_event.partner_event_id,
        )
        http_status_codes.append(200 if identity in seen_identities else 201)
        seen_identities.add(identity)
    return tuple(http_status_codes)


def execute_correctness_scenario(
    scenario: CorrectnessScenario,
    *,
    seed: int,
    configuration: GeneratorConfiguration,
    output_root: Path,
    trackrelay_client: httpx.Client,
    downstream_client: httpx.Client,
    downstream_url: str,
    sessions: sessionmaker[Session] = default_session_factory,
    test_run_id: UUID | None = None,
) -> ScenarioRunArtifacts:
    """Execute, save, and reconcile one correctness scenario."""
    manifest = build_correctness_manifest(
        scenario,
        seed=seed,
        configuration=configuration,
        test_run_id=test_run_id or uuid4(),
    )
    run_directory = output_root / scenario.value / str(manifest.test_run_id)
    run_directory.mkdir(parents=True, exist_ok=False)
    manifest_path = run_directory / "input-manifest.json"
    observations_path = run_directory / "request-observations.json"
    summary_path = run_directory / "database-summary.json"
    reconciliation_path = run_directory / "reconciliation.json"
    write_input_manifest(manifest, manifest_path)
    _prepare_database_for_run(manifest, sessions=sessions)

    simulator_mode = (
        SimulatorMode.UNAVAILABLE
        if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE
        else SimulatorMode.HEALTHY
    )
    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": simulator_mode.value},
    )
    mode_response.raise_for_status()

    request_observations = []
    try:
        for manifest_event in manifest.expected_events:
            response = trackrelay_client.post(
                f"/api/v1/partners/{manifest_event.partner_id}/events",
                headers={"X-Test-Run-ID": str(manifest.test_run_id)},
                json=manifest_event.payload,
            )
            request_observations.append(
                ScenarioRequestObservation(
                    sequence_number=manifest_event.sequence_number,
                    partner_id=manifest_event.partner_id,
                    partner_event_id=manifest_event.partner_event_id,
                    http_status_code=response.status_code,
                    http_response_body=response.json(),
                )
            )
    finally:
        reset_response = downstream_client.put(
            "/control/mode",
            json={"mode": SimulatorMode.HEALTHY.value},
        )
        reset_response.raise_for_status()

    observations = ScenarioObservations(
        scenario_name=scenario,
        test_run_id=manifest.test_run_id,
        requests=tuple(request_observations),
    )
    _write_observations(observations, observations_path)
    _complete_database_run(manifest.test_run_id, sessions=sessions)

    simulator_receipts = fetch_simulator_receipts(
        downstream_url,
        client=downstream_client,
    )
    with sessions() as session:
        reconciliation = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )
    write_reconciliation_report(reconciliation, reconciliation_path)

    summary_response = trackrelay_client.get(
        f"/api/v1/test-runs/{manifest.test_run_id}/summary"
    )
    summary_response.raise_for_status()
    _write_json_response(summary_response, summary_path)

    observed_http_status_codes = tuple(
        observation.http_status_code for observation in observations.requests
    )
    expected_http_status_codes = _expected_http_status_codes(
        scenario,
        manifest,
    )
    if observed_http_status_codes != expected_http_status_codes:
        raise ScenarioExecutionError(
            "HTTP outcomes disagreed with the scenario: "
            f"expected {expected_http_status_codes}, "
            f"observed {observed_http_status_codes}"
        )
    if not reconciliation.invariants_passed:
        raise ScenarioExecutionError(
            "Reconciliation invariants failed; inspect "
            f"{reconciliation_path}"
        )

    return ScenarioRunArtifacts(
        run_directory=run_directory,
        manifest_path=manifest_path,
        observations_path=observations_path,
        summary_path=summary_path,
        reconciliation_path=reconciliation_path,
        reconciliation=reconciliation,
    )


def build_parser() -> ArgumentParser:
    """Describe the repeatable correctness-scenario command line."""
    settings = Settings()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        type=CorrectnessScenario,
        choices=tuple(CorrectnessScenario),
    )
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--shipments", type=int, default=1)
    parser.add_argument("--partner-id", default="alpha-indonesia")
    parser.add_argument("--start-at", default=DEFAULT_START_AT.isoformat())
    parser.add_argument("--test-run-id", type=UUID)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--downstream-url", default=settings.downstream_url)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("results/correctness"),
    )
    return parser


def main() -> None:
    """Run one scenario against local services and print its artifacts."""
    parser = build_parser()
    arguments = parser.parse_args()
    configuration = GeneratorConfiguration(
        partner_id=arguments.partner_id,
        shipment_count=arguments.shipments,
        start_at=arguments.start_at,
    )

    try:
        with (
            httpx.Client(base_url=arguments.api_url, timeout=10.0) as api_client,
            httpx.Client(
                base_url=arguments.downstream_url,
                timeout=10.0,
            ) as downstream_client,
        ):
            artifacts = execute_correctness_scenario(
                arguments.scenario,
                seed=arguments.seed,
                configuration=configuration,
                output_root=arguments.output_root,
                trackrelay_client=api_client,
                downstream_client=downstream_client,
                downstream_url=arguments.downstream_url,
                test_run_id=arguments.test_run_id,
            )
    except (httpx.HTTPError, ScenarioExecutionError, ValueError) as error:
        parser.error(str(error))

    print(f"Scenario run: {artifacts.run_directory}")
    print(f"Reconciliation passed: {artifacts.reconciliation.invariants_passed}")


if __name__ == "__main__":
    main()
