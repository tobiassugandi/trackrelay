"""In-platform, high-resolution demand telemetry (never a scaling controller)."""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from threading import Event as StopEvent
from threading import Thread
from time import time

import boto3
from botocore.config import Config
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from trackrelay.config import Settings
from trackrelay.database import create_database_engine
from trackrelay.domain import DeliveryAttemptResult
from trackrelay.models import DeliveryAttempt, Event

NAMESPACE = "TrackRelay/Elasticity"
PERIOD_SECONDS = 10
logger = logging.getLogger(__name__)


def demand_snapshot(session: Session, observed_at: datetime) -> tuple[float, int]:
    """Global gauges: unique persisted arrivals/10s and unfinished logical events.

    Every API replica reads the same database; consumers must use Maximum, never
    Sum. A successful retry counts as one completed event, not another arrival.
    One statement keeps the counts in the same database snapshot.
    """
    arrivals = (
        select(func.count())
        .select_from(Event)
        .where(
            Event.created_at >= observed_at - timedelta(seconds=PERIOD_SECONDS),
            Event.created_at < observed_at,
        )
        .scalar_subquery()
    )
    persisted = select(func.count()).select_from(Event).scalar_subquery()
    delivered = (
        select(func.count(distinct(DeliveryAttempt.event_id)))
        .where(DeliveryAttempt.result == DeliveryAttemptResult.DELIVERED)
        .scalar_subquery()
    )
    recent, outstanding = session.execute(select(arrivals, persisted - delivered)).one()
    return recent / PERIOD_SECONDS, max(0, outstanding)


def publish_snapshot(
    client, queue_name: str, observed_at: datetime, snapshot: tuple[float, int]
) -> None:
    """Publish real zeroes, but never fabricate zeroes after a collection failure."""
    rate, outstanding = snapshot
    client.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [{"Name": "QueueName", "Value": queue_name}],
                "Timestamp": observed_at,
                "Value": value,
                "Unit": unit,
                "StorageResolution": 1,
            }
            for name, value, unit in (
                ("ArrivalRate", rate, "Count/Second"),
                ("OutstandingEvents", outstanding, "Count"),
            )
        ],
    )


def run_publisher(stop: StopEvent, session_factory, client, queue_name: str) -> None:
    """Sample on ten-second boundaries, skipping missed slots instead of replaying."""
    while not stop.wait(PERIOD_SECONDS - time() % PERIOD_SECONDS):
        try:
            observed_at = datetime.now(UTC)
            with session_factory() as session:
                snapshot = demand_snapshot(session, observed_at)
            publish_snapshot(client, queue_name, observed_at, snapshot)
        except Exception:
            logger.exception("High-resolution scaling telemetry unavailable")


@asynccontextmanager
async def scaling_metrics_lifespan(app):
    """Own a bounded, independent publisher; local/test deployments stay disabled."""
    settings = Settings()
    if not settings.scaling_metrics_queue_name:
        yield
        return
    # A separate pool prevents telemetry from taking an ingestion pool slot.
    # Connection and statement timeouts also bound shutdown on a database outage.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = settings.database_connection_url()
    engine = (
        create_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=3,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
        )
        if url.startswith("postgresql")
        else create_database_engine(url)
    )
    client = boto3.client(
        "cloudwatch",
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}
        ),
    )
    stop = StopEvent()
    thread = Thread(
        target=run_publisher,
        args=(stop, sessionmaker(engine), client, settings.scaling_metrics_queue_name),
        name="scaling-metrics",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # Do not block the application's event loop while the bounded calls finish.
        from asyncio import to_thread

        await to_thread(thread.join)
        client.close()
        engine.dispose()
