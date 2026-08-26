"""Tests for the private synchronous-rehost experiment helper."""

from base64 import b64decode
from uuid import UUID

import httpx

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    EVIDENCE_PREFIX,
    RehostServerEvidence,
    RehostWorkloadPoint,
    encoded_evidence,
    prepare_rehost_workload_point,
)
from trackrelay.models import Partner
from trackrelay.models import TestRun as ExperimentRunModel

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000901")


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def test_point_regenerates_the_frozen_healthy_manifest() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=2,
        duration_seconds=3,
        partner_id="load-alpha",
    )

    manifest = point.manifest()

    assert manifest.test_run_id == TEST_RUN_ID
    assert manifest.scenario_name == "healthy-baseline"
    assert manifest.events_generated == 6
    assert len(manifest.expected_final_shipments) == 6


def test_prepare_creates_private_run_state_and_sets_healthy_mode() -> None:
    engine, sessions = create_test_database()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"mode": "HEALTHY", "delay_seconds": 0},
        )

    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=2,
        duration_seconds=3,
        partner_id="load-alpha",
    )
    with httpx.Client(
        base_url="http://downstream:8001",
        transport=httpx.MockTransport(respond),
    ) as client:
        prepare_rehost_workload_point(
            point,
            sessions=sessions,
            downstream_client=client,
        )

    assert requests[0].url.path == "/control/mode"
    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        test_run = session.get(ExperimentRunModel, TEST_RUN_ID)
        assert partner is not None
        assert partner.adapter_type == "courier-alpha"
        assert test_run is not None
        assert test_run.expected_event_count == 6
    engine.dispose()


def test_evidence_line_round_trips_one_compact_json_object() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=1,
        duration_seconds=1,
        partner_id="load-alpha",
    )
    evidence = RehostServerEvidence(
        point=point,
        reconciliation=ReconciliationReport(
            test_run_id=TEST_RUN_ID,
            generated=1,
            accepted=1,
            rejected=0,
            unique=1,
            processed=1,
            failed=0,
            pending=0,
            unaccounted=0,
            simulator_receipts=1,
            simulator_unique_events=1,
        ),
    )

    line = encoded_evidence(evidence)
    decoded = b64decode(line.removeprefix(EVIDENCE_PREFIX)).decode("utf-8")

    assert RehostServerEvidence.model_validate_json(decoded) == evidence
