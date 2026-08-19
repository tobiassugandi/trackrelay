"""Prepare local database identities used by performance experiments."""

from argparse import ArgumentParser

from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory as default_session_factory
from trackrelay.models import Partner

LOAD_ADAPTER_TYPE = "courier-alpha"


def prepare_load_partner(
    partner_id: str,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
) -> None:
    """Create or validate the active Courier Alpha load-test partner."""
    if not partner_id.strip():
        raise ValueError("load partner ID must not be blank")

    with sessions.begin() as session:
        database_partner = session.get(Partner, partner_id)
        if database_partner is None:
            session.add(
                Partner(
                    id=partner_id,
                    name=f"Load test {partner_id}",
                    adapter_type=LOAD_ADAPTER_TYPE,
                    is_active=True,
                )
            )
            return

        if (
            database_partner.adapter_type != LOAD_ADAPTER_TYPE
            or not database_partner.is_active
        ):
            raise ValueError(
                "load partner must be active and use courier-alpha"
            )


def build_parser() -> ArgumentParser:
    """Describe the local load-partner preparation command."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--partner-id", default="load-alpha")
    return parser


def main() -> None:
    """Prepare the configured partner before k6 sends ingestion traffic."""
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        prepare_load_partner(arguments.partner_id)
    except ValueError as error:
        parser.error(str(error))
    print(f"Load partner ready: {arguments.partner_id}")


if __name__ == "__main__":
    main()
