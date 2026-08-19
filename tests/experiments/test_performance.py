"""Tests for local performance-experiment preparation."""

from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.experiments.performance import prepare_load_partner
from trackrelay.models import Partner


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
