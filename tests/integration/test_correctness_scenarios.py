"""PostgreSQL-backed coverage for repeatable correctness commands."""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from pytest import fixture, mark
from sqlalchemy import delete, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.downstream.control import SimulatorMode
from trackrelay.downstream.main import app as downstream_app
from trackrelay.downstream.main import event_store, simulator_control
from trackrelay.experiments.generator import GeneratorConfiguration
from trackrelay.experiments.scenarios import (
    CorrectnessScenario,
    ScenarioObservations,
    execute_correctness_scenario,
)
from trackrelay.main import app as trackrelay_app
from trackrelay.main import get_event_deliverer
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.services import DeliveryResult, deliver_and_record_normalized_event

PARTNER_ID = "correctness-scenario-alpha"
TEST_RUN_ID_BY_SCENARIO = {
    CorrectnessScenario.NORMAL: UUID(
        "00000000-0000-0000-0000-000000000811"
    ),
    CorrectnessScenario.DUPLICATE: UUID(
        "00000000-0000-0000-0000-000000000812"
    ),
    CorrectnessScenario.OUT_OF_ORDER: UUID(
        "00000000-0000-0000-0000-000000000813"
    ),
    CorrectnessScenario.DOWNSTREAM_OUTAGE: UUID(
        "00000000-0000-0000-0000-000000000814"
    ),
}


def cleanup_scenario_records() -> None:
    test_run_ids = tuple(TEST_RUN_ID_BY_SCENARIO.values())
    with session_factory.begin() as session:
        tracking_numbers = tuple(
            session.scalars(
                select(Event.tracking_number)
                .where(Event.test_run_id.in_(test_run_ids))
                .distinct()
            )
        )
        scenario_event_ids = select(Event.id).where(
            Event.test_run_id.in_(test_run_ids)
        )
        session.execute(
            delete(DeliveryAttempt).where(
                DeliveryAttempt.event_id.in_(scenario_event_ids)
            )
        )
        session.execute(delete(Event).where(Event.test_run_id.in_(test_run_ids)))
        if tracking_numbers:
            session.execute(
                delete(Shipment).where(
                    Shipment.tracking_number.in_(tracking_numbers)
                )
            )
        session.execute(
            delete(ExperimentRunModel).where(
                ExperimentRunModel.id.in_(test_run_ids)
            )
        )
        session.execute(delete(Partner).where(Partner.id == PARTNER_ID))


@fixture
def scenario_clients() -> Iterator[tuple[TestClient, TestClient]]:
    assert engine.dialect.name == "postgresql"
    cleanup_scenario_records()
    event_store.clear()
    simulator_control.reset()

    with TestClient(
        downstream_app,
        base_url="http://downstream.test",
    ) as downstream_client:

        def deliver_to_simulator(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_event_deliverer] = (
            lambda: deliver_to_simulator
        )
        with TestClient(
            trackrelay_app,
            base_url="http://trackrelay.test",
        ) as trackrelay_client:
            yield trackrelay_client, downstream_client

    trackrelay_app.dependency_overrides.clear()
    cleanup_scenario_records()
    event_store.clear()
    simulator_control.reset()


@mark.integration
@mark.parametrize("scenario", tuple(CorrectnessScenario))
def test_correctness_scenario_command_saves_and_reconciles_its_run(
    scenario: CorrectnessScenario,
    scenario_clients: tuple[TestClient, TestClient],
    tmp_path: Path,
) -> None:
    trackrelay_client, downstream_client = scenario_clients

    artifacts = execute_correctness_scenario(
        scenario,
        seed=20260818,
        configuration=GeneratorConfiguration(
            partner_id=PARTNER_ID,
            shipment_count=1,
        ),
        output_root=tmp_path,
        trackrelay_client=trackrelay_client,
        downstream_client=downstream_client,
        downstream_url="http://downstream.test",
        test_run_id=TEST_RUN_ID_BY_SCENARIO[scenario],
    )

    assert artifacts.manifest_path.is_file()
    assert artifacts.observations_path.is_file()
    assert artifacts.summary_path.is_file()
    assert artifacts.reconciliation_path.is_file()
    assert artifacts.reconciliation.invariants_passed is True
    assert artifacts.reconciliation.rejected == 0
    assert artifacts.reconciliation.unaccounted == 0
    assert artifacts.reconciliation.duplicate_business_effects == 0
    assert artifacts.reconciliation.incorrect_final_shipment_states == 0

    expected_request_count = (
        10 if scenario is CorrectnessScenario.DUPLICATE else 5
    )
    expected_simulator_receipts = (
        0 if scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE else 5
    )
    assert artifacts.reconciliation.generated == expected_request_count
    assert artifacts.reconciliation.accepted == expected_request_count
    assert artifacts.reconciliation.unique == 5
    assert artifacts.reconciliation.processed == 5
    assert (
        artifacts.reconciliation.simulator_receipts
        == expected_simulator_receipts
    )

    observations = ScenarioObservations.model_validate_json(
        artifacts.observations_path.read_text(encoding="utf-8")
    )
    observed_status_codes = [
        observation.http_status_code for observation in observations.requests
    ]
    if scenario is CorrectnessScenario.DUPLICATE:
        assert observed_status_codes == [201] * 5 + [200] * 5
    elif scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE:
        assert observed_status_codes == [502] * 5
    else:
        assert observed_status_codes == [201] * 5

    database_summary = json.loads(
        artifacts.summary_path.read_text(encoding="utf-8")
    )
    assert database_summary["scenario_name"] == scenario.value
    assert database_summary["declared_event_count"] == expected_request_count
    assert database_summary["database_events"]["persisted"] == 5
    assert database_summary["database_events"]["processed"] == 5
    assert database_summary["database_delivery_attempts"]["total"] == 5

    with session_factory() as session:
        database_test_run = session.get(
            ExperimentRunModel,
            TEST_RUN_ID_BY_SCENARIO[scenario],
        )
        tracking_number = session.scalar(
            select(Event.tracking_number).where(
                Event.test_run_id == TEST_RUN_ID_BY_SCENARIO[scenario]
            )
        )
        database_shipment = session.get(Shipment, tracking_number)

        assert database_test_run is not None
        assert database_test_run.completed_at is not None
        assert database_shipment is not None
        assert database_shipment.current_status is ShipmentStatus.DELIVERED

    assert simulator_control.get_mode() is SimulatorMode.HEALTHY
