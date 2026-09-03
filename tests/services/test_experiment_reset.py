"""Tests for the exact synthetic-state reset boundary."""

from datetime import UTC, datetime
from json import loads
from pathlib import Path
from uuid import UUID

import httpx
from pytest import mark, raises
from sqlalchemy import inspect

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    ShipmentStatus,
)
from trackrelay.downstream.control import SimulatorMode
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Partner,
    Shipment,
)
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.services.experiment_reset import (
    ExperimentResetError,
    database_experiment_counts,
    reset_experiment_state,
)

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000947")
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def populated_sessions(tmp_path: Path, *, database_url: str | None = None):
    engine = create_database_engine(
        database_url or f"sqlite+pysqlite:///{tmp_path / 'reset.db'}"
    )
    # The optional PostgreSQL contract test must never reuse existing tables.
    assert not inspect(engine).get_table_names(), (
        "reset tests require an empty database"
    )
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            Partner(
                id="reset-alpha",
                name="Reset Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            ExperimentRunModel(
                id=TEST_RUN_ID,
                scenario_name="elasticity-fixed-control",
                random_seed=20260901,
                configuration={"partner_id": "reset-alpha"},
                expected_event_count=2,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        session.add_all(
            [
                Shipment(
                    tracking_number=f"RESET-{index}",
                    current_status=ShipmentStatus.CREATED,
                    current_status_occurred_at=NOW,
                )
                for index in (1, 2)
            ]
        )
        session.flush()
        events = [
            Event(
                partner_id="reset-alpha",
                partner_event_id=f"RESET-EVENT-{index}",
                tracking_number=f"RESET-{index}",
                status=ShipmentStatus.CREATED,
                occurred_at=NOW,
                received_at=NOW,
                raw_payload={"index": index},
                test_run_id=TEST_RUN_ID,
                processing_status=EventProcessingStatus.PROCESSED,
                state_applied=True,
            )
            for index in (1, 2)
        ]
        session.add_all(events)
        session.flush()
        session.add_all(
            [
                DeliveryOutboxEntry(
                    event_id=event.id,
                    created_at=NOW,
                    published_at=NOW,
                )
                for event in events
            ]
        )
        session.add_all(
            [
                DeliveryAttempt(
                    event_id=event.id,
                    attempt_number=1,
                    result=DeliveryAttemptResult.DELIVERED,
                    response_code=202,
                    latency_ms=1,
                    started_at=NOW,
                    completed_at=NOW,
                )
                for event in events
            ]
        )
    return sessions


def simulator_transport(
    *, initial_mode: SimulatorMode = SimulatorMode.HEALTHY
) -> httpx.MockTransport:
    state = {"mode": initial_mode, "events": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/control/status":
            return httpx.Response(
                200,
                json={"mode": state["mode"], "delay_seconds": 0},
                request=request,
            )
        if request.method == "GET" and request.url.path == "/control/events/count":
            return httpx.Response(
                200,
                json={"event_count": state["events"]},
                request=request,
            )
        if request.method == "PUT" and request.url.path == "/control/mode":
            assert loads(request.content) == {"mode": SimulatorMode.HEALTHY.value}
            state["mode"] = SimulatorMode.HEALTHY
            return httpx.Response(
                200,
                json={"mode": SimulatorMode.HEALTHY.value, "delay_seconds": 0},
                request=request,
            )
        if request.method == "DELETE" and request.url.path == "/control/events":
            cleared = state["events"]
            state["events"] = 0
            return httpx.Response(
                200,
                json={"cleared_event_count": cleared},
                request=request,
            )
        raise AssertionError((request.method, request.url.path))

    return httpx.MockTransport(handler)


def test_reset_removes_every_synthetic_row_and_preserves_partner(
    tmp_path: Path,
) -> None:
    sessions = populated_sessions(tmp_path)
    with (
        sessions() as session,
        httpx.Client(
            base_url="http://simulator",
            transport=simulator_transport(),
        ) as downstream_client,
    ):
        evidence = reset_experiment_state(
            session=session,
            downstream_client=downstream_client,
            test_run_id=TEST_RUN_ID,
            expected_event_count=2,
        )

        assert evidence.before.database.delivery_outbox_entries == 2
        assert evidence.before.simulator_mode is SimulatorMode.HEALTHY
        assert evidence.after.empty_and_healthy
        assert database_experiment_counts(session).empty
        assert session.get(Partner, "reset-alpha") is not None


def test_reset_refuses_a_mismatched_run_without_deleting_database_state(
    tmp_path: Path,
) -> None:
    sessions = populated_sessions(tmp_path)
    with (
        sessions() as session,
        httpx.Client(
            base_url="http://simulator",
            transport=simulator_transport(),
        ) as downstream_client,
        raises(ExperimentResetError, match="differs from the approved"),
    ):
        reset_experiment_state(
            session=session,
            downstream_client=downstream_client,
            test_run_id=UUID(int=1),
            expected_event_count=2,
        )

    with sessions() as session:
        assert database_experiment_counts(session).events == 2


def test_reset_refuses_degraded_simulator_state_without_deleting_database_state(
    tmp_path: Path,
) -> None:
    sessions = populated_sessions(tmp_path)
    with (
        sessions() as session,
        httpx.Client(
            base_url="http://simulator",
            transport=simulator_transport(initial_mode=SimulatorMode.SLOW),
        ) as downstream_client,
        raises(ExperimentResetError, match="differs from the approved"),
    ):
        reset_experiment_state(
            session=session,
            downstream_client=downstream_client,
            test_run_id=TEST_RUN_ID,
            expected_event_count=2,
        )

    with sessions() as session:
        assert database_experiment_counts(session).events == 2


@mark.parametrize("mode", ["healthy", "unknown", None, {"mode": "HEALTHY"}])
def test_reset_refuses_invalid_wire_modes_without_deleting_state(tmp_path, mode):
    sessions = populated_sessions(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.method)
        body = (
            {"mode": mode}
            if request.url.path == "/control/status"
            else {"event_count": 2}
        )
        return httpx.Response(200, json=body, request=request)

    with (
        sessions() as session,
        httpx.Client(
            base_url="http://simulator", transport=httpx.MockTransport(handler)
        ) as downstream_client,
        raises(ExperimentResetError, match="invalid reset state"),
    ):
        reset_experiment_state(
            session=session,
            downstream_client=downstream_client,
            test_run_id=TEST_RUN_ID,
            expected_event_count=2,
        )
    assert calls == ["GET", "GET"]
    with sessions() as session:
        assert database_experiment_counts(session).events == 2
