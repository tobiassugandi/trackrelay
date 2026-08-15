"""Tests for the repeatable experiment-run persistence model."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pytest import raises
from sqlalchemy.exc import IntegrityError

from trackrelay.database import create_database_engine, create_session_factory
from trackrelay.models import TestRun as ExperimentRunModel

RUN_ID = UUID("00000000-0000-0000-0000-000000000701")
STARTED_AT = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    ExperimentRunModel.__table__.create(engine)
    return engine, create_session_factory(engine)


def test_test_run_round_trip_keeps_the_reproducible_definition() -> None:
    engine, sessions = create_test_database()
    completed_at = STARTED_AT + timedelta(minutes=2)

    with sessions.begin() as session:
        session.add(
            ExperimentRunModel(
                id=RUN_ID,
                scenario_name="normal",
                random_seed=8675309,
                configuration={"events": 25, "partner": "alpha-indonesia"},
                expected_event_count=25,
                started_at=STARTED_AT,
                completed_at=completed_at,
            )
        )

    with sessions() as session:
        test_run = session.get(ExperimentRunModel, RUN_ID)
        assert test_run is not None
        assert test_run.scenario_name == "normal"
        assert test_run.random_seed == 8675309
        assert test_run.configuration == {
            "events": 25,
            "partner": "alpha-indonesia",
        }
        assert test_run.expected_event_count == 25
        assert test_run.started_at.replace(tzinfo=UTC) == STARTED_AT
        assert test_run.completed_at is not None
        assert test_run.completed_at.replace(tzinfo=UTC) == completed_at

    engine.dispose()


def test_test_run_rejects_a_negative_expected_event_count() -> None:
    engine, sessions = create_test_database()

    with sessions() as session:
        session.add(
            ExperimentRunModel(
                scenario_name="invalid",
                random_seed=1,
                configuration={},
                expected_event_count=-1,
            )
        )
        with raises(IntegrityError):
            session.commit()

    engine.dispose()
