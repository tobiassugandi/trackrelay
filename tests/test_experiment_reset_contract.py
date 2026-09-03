"""Exercise real reset HTTP contracts, not independently invented JSON mocks."""

from datetime import UTC, timedelta
from json import dumps
from os import environ
from types import SimpleNamespace
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from pytest import fixture, mark, skip
from sqlalchemy import select
from sqlalchemy.engine import make_url

from tests.services.test_experiment_reset import TEST_RUN_ID, populated_sessions
from tests.test_aws_experiment_reset import (
    API_URL,
    CLUSTER_NAME,
    DLQ_URL,
    SOURCE_QUEUE_URL,
    STARTED_AT,
    WORKER_SERVICE,
    completed,
    ready_session,
)
from trackrelay import main as api_module
from trackrelay.aws_experiment_reset import execute_experiment_reset
from trackrelay.domain import NormalizedEvent
from trackrelay.downstream import main as simulator
from trackrelay.downstream.control import SimulatorMode
from trackrelay.models import Event, Partner
from trackrelay.services.experiment_reset import (
    ExperimentResetEvidence,
    ExperimentStateSnapshot,
    database_experiment_counts,
)


@fixture
def connected_apps(tmp_path, monkeypatch, request):
    database_url = None
    if getattr(request, "param", None) == "postgresql":
        database_url = environ.get("TRACKRELAY_RESET_TEST_DATABASE_URL")
        if database_url is None:
            skip("requires a fresh local TRACKRELAY_RESET_TEST_DATABASE_URL")
        url = make_url(database_url)
        assert (
            url.drivername == "postgresql+psycopg"
            and url.host in {"127.0.0.1", "localhost"}
            and url.database == "trackrelay_reset_contract"
            and not url.query
        ), "only a dedicated, fresh local reset-contract database is allowed"
    sessions = populated_sessions(tmp_path, database_url=database_url)
    simulator.event_store.clear()
    simulator.simulator_control.reset()
    calls = []
    with sessions() as session:
        for event in session.scalars(select(Event)):
            simulator.event_store.record(
                NormalizedEvent(
                    partner_id=event.partner_id,
                    partner_event_id=event.partner_event_id,
                    tracking_number=event.tracking_number,
                    status=event.status,
                    occurred_at=event.occurred_at.replace(tzinfo=UTC),
                    received_at=event.received_at.replace(tzinfo=UTC),
                    raw_payload=event.raw_payload,
                    test_run_id=event.test_run_id,
                )
            )

    def database_session():
        with sessions() as session:
            yield session

    class SimulatorClient(TestClient):
        def request(self, method, url, **kwargs):
            calls.append((method, str(url), kwargs.get("json")))
            return super().request(method, url, **kwargs)

    original_overrides = dict(api_module.app.dependency_overrides)
    api_module.app.dependency_overrides[api_module.get_session] = database_session
    # Replace only this module's client factory. Both ASGI apps still execute
    # their actual request validation, handlers, response models and statuses.
    monkeypatch.setattr(
        api_module,
        "httpx",
        SimpleNamespace(
            Client=lambda **_kwargs: SimulatorClient(simulator.app),
            HTTPError=httpx.HTTPError,
        ),
    )
    try:
        with TestClient(api_module.app) as client:
            yield client, sessions, calls
    finally:
        api_module.app.dependency_overrides.clear()
        api_module.app.dependency_overrides.update(original_overrides)
        simulator.event_store.clear()
        simulator.simulator_control.reset()
        sessions.kw["bind"].dispose()


@mark.parametrize("mode", tuple(SimulatorMode))
def test_state_reads_every_real_simulator_mode(connected_apps, mode):
    client, _, _ = connected_apps
    simulator.simulator_control.set_mode(mode)

    response = client.get("/api/v1/experiments/state")

    assert response.status_code == 200
    assert response.json()["simulator_mode"] == mode.value
    snapshot = ExperimentStateSnapshot.model_validate(response.json())
    assert snapshot.simulator_mode is mode
    assert snapshot.simulator_receipts == snapshot.database.events == 2


def test_real_reset_clears_both_stores_and_preserves_partner(connected_apps):
    client, sessions, calls = connected_apps

    response = client.post(
        "/api/v1/experiments/reset",
        json={"test_run_id": str(TEST_RUN_ID), "expected_event_count": 2},
    )

    assert response.status_code == 200
    evidence = ExperimentResetEvidence.model_validate(response.json())
    assert evidence.before.simulator_mode is SimulatorMode.HEALTHY
    assert evidence.after.empty_and_healthy
    assert evidence.simulator_receipts_removed == 2
    assert ("PUT", "/control/mode", {"mode": "HEALTHY"}) in calls
    assert ("DELETE", "/control/events", None) in calls
    with sessions() as session:
        assert database_experiment_counts(session).empty
        assert session.get(Partner, "reset-alpha") is not None
    assert not simulator.event_store.all()


@mark.parametrize(
    "mode", [mode for mode in SimulatorMode if mode != SimulatorMode.HEALTHY]
)
def test_real_reset_refuses_degraded_modes_without_writes(connected_apps, mode):
    client, sessions, calls = connected_apps
    simulator.simulator_control.set_mode(mode)

    response = client.post(
        "/api/v1/experiments/reset",
        json={"test_run_id": str(TEST_RUN_ID), "expected_event_count": 2},
    )

    assert response.status_code == 409
    assert all(method == "GET" for method, _, _ in calls)
    with sessions() as session:
        assert database_experiment_counts(session).events == 2
    assert len(simulator.event_store.all()) == 2
    assert simulator.simulator_control.get_mode() is mode


def test_real_reset_refuses_another_run_without_writes(connected_apps):
    client, sessions, calls = connected_apps
    response = client.post(
        "/api/v1/experiments/reset",
        json={"test_run_id": str(UUID(int=1)), "expected_event_count": 2},
    )
    assert response.status_code == 409
    assert all(method == "GET" for method, _, _ in calls)
    with sessions() as session:
        assert database_experiment_counts(session).events == 2
    assert len(simulator.event_store.all()) == 2


def test_controller_resets_real_apps_and_validates_serialized_empty_evidence(
    connected_apps,
    tmp_path,
):
    client, _, calls = connected_apps
    session = ready_session(tmp_path)
    evidence_root = tmp_path / "reset"
    evidence_root.mkdir()
    current_time = STARTED_AT
    purged = []

    def sleeper(seconds):
        nonlocal current_time
        current_time += timedelta(seconds=seconds)

    def runner(arguments, _input):
        if "get-queue-attributes" in arguments:
            names = arguments[
                arguments.index("--attribute-names") + 1 : arguments.index("--query")
            ]
            return completed(arguments, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in arguments:
            return completed(arguments, stdout="[1, 1, 0]")
        if "describe-scalable-targets" in arguments:
            return completed(arguments, stdout="[]")
        if "purge-queue" in arguments:
            purged.append(arguments[arguments.index("--queue-url") + 1])
            return completed(arguments)
        raise AssertionError(arguments)

    result = execute_experiment_reset(
        session,
        fixed_summary=SimpleNamespace(
            measurement=SimpleNamespace(
                test_run_id=TEST_RUN_ID,
                definition=SimpleNamespace(expected_request_count=2),
            )
        ),
        api_url=API_URL,
        source_queue_url=SOURCE_QUEUE_URL,
        dead_letter_queue_url=DLQ_URL,
        cluster_name=CLUSTER_NAME,
        worker_service_name=WORKER_SERVICE,
        evidence_root=evidence_root,
        client=client,
        runner=runner,
        now=lambda: current_time,
        sleeper=sleeper,
    )

    assert purged == [SOURCE_QUEUE_URL, DLQ_URL]
    assert result.application_reset.after.empty_and_healthy
    assert result.observations[-1].seconds_after_purge == 90
    assert (
        type(result).model_validate_json((evidence_root / "result.json").read_text())
        == result
    )
    assert sum(method == "DELETE" for method, _, _ in calls) == 1


@mark.integration
@mark.parametrize("connected_apps", ["postgresql"], indirect=True)
def test_postgresql_controller_resets_real_apps(connected_apps, tmp_path):
    """Exercise real PostgreSQL TRUNCATE instead of SQLite's DELETE fallback."""
    test_controller_resets_real_apps_and_validates_serialized_empty_evidence(
        connected_apps,
        tmp_path,
    )
