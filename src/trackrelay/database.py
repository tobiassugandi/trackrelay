"""Database engine, sessions, and connectivity checks."""

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from trackrelay.config import Settings


class Base(DeclarativeBase):
    """Base class whose metadata Alembic uses for migrations."""


def create_database_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> Engine:
    """Create an engine without opening a database connection yet."""
    engine_arguments: dict[str, object] = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        engine_arguments.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
        )
    return create_engine(database_url, **engine_arguments)


def create_session_factory(database_engine: Engine) -> sessionmaker[Session]:
    """Create sessions bound to the supplied engine."""
    return sessionmaker(bind=database_engine, autoflush=False, expire_on_commit=False)


settings = Settings()
engine = create_database_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
)
session_factory = create_session_factory(engine)


def get_session() -> Iterator[Session]:
    """Yield one session and always close it afterward."""
    with session_factory() as session:
        yield session


def check_database_connection(database_engine: Engine = engine) -> bool:
    """Return whether the database can execute a minimal query."""
    with database_engine.connect() as connection:
        return connection.scalar(text("SELECT 1")) == 1


def main() -> None:
    """Run the connection check from the command line."""
    if not check_database_connection():
        raise SystemExit("Database connection check returned an unexpected result.")
    print("Database connection: ok")


if __name__ == "__main__":
    main()
