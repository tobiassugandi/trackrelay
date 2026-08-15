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
                id="alpha-indonesia",
                name="Alpha Indonesia",
                adapter_type="courier-alpha",
            )
        )
        session.commit()

    with session_factory() as session:
        partner = session.get(Partner, "alpha-indonesia")
        assert partner is not None
        assert partner.id == "alpha-indonesia"
        assert partner.name == "Alpha Indonesia"
        assert partner.adapter_type == "courier-alpha"
        assert partner.is_active is True

    engine.dispose()
