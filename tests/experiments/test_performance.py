"""Tests for local performance-experiment preparation."""

from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import ShipmentStatus
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    build_k6_command,
    build_performance_manifest,
    prepare_load_partner,
)
from trackrelay.models import Partner

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000805")


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def test_prepare_load_partner_creates_an_active_alpha_partner() -> None:
    engine, sessions = create_test_database()

    prepare_load_partner("load-alpha", sessions=sessions)

    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        assert partner is not None
        assert partner.name == "Load test load-alpha"
        assert partner.adapter_type == "courier-alpha"
        assert partner.is_active is True
    engine.dispose()


def test_prepare_load_partner_accepts_an_existing_compatible_partner() -> None:
    engine, sessions = create_test_database()
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Existing load partner",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )

    prepare_load_partner("load-alpha", sessions=sessions)

    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        assert partner is not None
        assert partner.name == "Existing load partner"
    engine.dispose()


def test_prepare_load_partner_rejects_an_incompatible_partner() -> None:
    engine, sessions = create_test_database()
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Wrong adapter",
                adapter_type="courier-beta",
                is_active=True,
            )
        )

    with raises(
        ValueError,
        match="load partner must be active and use courier-alpha",
    ):
        prepare_load_partner("load-alpha", sessions=sessions)
    engine.dispose()


def test_slow_configuration_declares_every_repeatable_input() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.SLOW,
    )

    assert configuration.expected_request_count == 25
    assert configuration.simulator_mode == "SLOW"
    assert configuration.expected_http_status_code == 201
    assert configuration.model_dump(mode="json")["k6_image"] == (
        "grafana/k6:2.1.0"
    )
    assert configuration.trackrelay_api_url == "http://127.0.0.1:8000"
    assert configuration.trackrelay_api_url_for_container == (
        "http://host.docker.internal:8000"
    )
    assert configuration.downstream_url == "http://127.0.0.1:8001"
    assert configuration.post_load_settle_timeout_seconds == 30
    assert configuration.post_load_stable_window_seconds == 2


def test_outage_configuration_expects_the_persisted_delivery_failure() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.OUTAGE,
    )

    assert configuration.simulator_mode == "UNAVAILABLE"
    assert configuration.expected_http_status_code == 502


def test_healthy_configuration_declares_the_baseline_condition() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
    )

    assert configuration.simulator_mode == "HEALTHY"
    assert configuration.expected_http_status_code == 201

    manifest = build_performance_manifest(
        configuration,
        test_run_id=TEST_RUN_ID,
    )

    assert manifest.scenario_name == "healthy-baseline"
    assert manifest.events_generated == configuration.expected_request_count
    assert len(manifest.expected_final_shipments) == (
        configuration.expected_request_count
    )
    assert set(manifest.expected_final_shipments.values()) == {
        ShipmentStatus.CREATED
    }


def test_configuration_requires_complete_five_event_histories() -> None:
    with raises(
        ValidationError,
        match="request rate times duration must be divisible by 5",
    ):
        PerformanceExperimentConfiguration(
            scenario=LoadScenario.SLOW,
            request_rate_per_second=2,
            duration_seconds=3,
        )

    healthy_configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
        request_rate_per_second=2,
        duration_seconds=3,
    )

    assert healthy_configuration.expected_request_count == 6


def test_performance_manifest_matches_the_scheduled_load() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.OUTAGE,
        request_rate_per_second=1,
        duration_seconds=5,
    )

    manifest = build_performance_manifest(
        configuration,
        test_run_id=TEST_RUN_ID,
    )

    assert manifest.test_run_id == TEST_RUN_ID
    assert manifest.scenario_name == "outage-under-load"
    assert manifest.events_generated == 5
    assert manifest.expected_unique_events == 5
    assert len(manifest.expected_final_shipments) == 1


def test_k6_command_mounts_the_manifest_and_run_directory() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.SLOW,
    )
    manifest_path = Path("/tmp/trackrelay-manifest.json")
    run_directory = Path("/tmp/trackrelay-performance-run")

    command = build_k6_command(
        configuration,
        manifest_path=manifest_path,
        run_directory=run_directory,
    )

    assert command[0:3] == ("docker", "run", "--rm")
    assert "LOAD_RATE=5" in command
    assert "LOAD_DURATION_SECONDS=5" in command
    assert "EXPECTED_HTTP_STATUS=201" in command
    assert f"{manifest_path.resolve()}:/input-manifest.json:ro" in command
    assert f"{run_directory.resolve()}:/results" in command
    assert command[-2:] == ("run", "/scripts/fixed-rate.js")
