"""Tests for the courier partner persistence model."""

from trackrelay.database import create_database_engine, create_session_factory
from trackrelay.models import Partner


def test_partner_model_round_trip() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Partner.__table__.create(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.commit()

    with session_factory() as session:
        partner = session.get(Partner, "courier-alpha")
        assert partner is not None
        assert partner.name == "Courier Alpha"
        assert partner.adapter_type == "courier-alpha"
        assert partner.is_active is True

    engine.dispose()
