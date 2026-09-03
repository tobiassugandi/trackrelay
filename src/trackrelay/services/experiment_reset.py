"""Clear and prove empty synthetic state between controlled treatments."""

from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from trackrelay.downstream.control import SimulatorMode
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Shipment,
)
from trackrelay.models import TestRun as ExperimentRunModel

NonNegativeInteger = Annotated[int, Field(ge=0)]


class ExperimentResetError(RuntimeError):
    """Synthetic state differs from the explicitly named reset boundary."""


class ExperimentDatabaseCounts(BaseModel):
    """All database rows owned by repeatable synthetic treatments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_attempts: NonNegativeInteger
    delivery_outbox_entries: NonNegativeInteger
    events: NonNegativeInteger
    shipments: NonNegativeInteger
    test_runs: NonNegativeInteger

    @property
    def empty(self) -> bool:
        return not any(self.model_dump().values())


class ExperimentStateSnapshot(BaseModel):
    """Compact database and simulator state without private endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    database: ExperimentDatabaseCounts
    simulator_receipts: NonNegativeInteger
    simulator_mode: SimulatorMode

    @property
    def empty_and_healthy(self) -> bool:
        return (
            self.database.empty
            and self.simulator_receipts == 0
            and self.simulator_mode is SimulatorMode.HEALTHY
        )


class ExperimentResetEvidence(BaseModel):
    """Application-owned proof of one exact destructive reset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    expected_event_count: NonNegativeInteger
    before: ExperimentStateSnapshot
    after: ExperimentStateSnapshot
    simulator_receipts_removed: NonNegativeInteger

    @model_validator(mode="after")
    def require_exact_empty_reset(self) -> "ExperimentResetEvidence":
        if self.before.database.test_runs != 1:
            raise ValueError("reset evidence must start with exactly one test run")
        if self.before.database.events != self.expected_event_count:
            raise ValueError("reset evidence starts with an unexpected event count")
        if self.before.database.delivery_outbox_entries != self.expected_event_count:
            raise ValueError("reset evidence starts with an unexpected outbox count")
        if self.before.database.shipments != self.expected_event_count:
            raise ValueError("reset evidence starts with an unexpected shipment count")
        if self.before.database.delivery_attempts < self.expected_event_count:
            raise ValueError("reset evidence starts without complete delivery attempts")
        if self.before.simulator_receipts != self.expected_event_count:
            raise ValueError("reset evidence starts with unexpected simulator receipts")
        if self.before.simulator_mode is not SimulatorMode.HEALTHY:
            raise ValueError("reset evidence must start with a healthy simulator")
        if self.simulator_receipts_removed != self.before.simulator_receipts:
            raise ValueError("simulator reset count differs from prior state")
        if not self.after.empty_and_healthy:
            raise ValueError("application reset did not leave empty healthy state")
        return self


def database_experiment_counts(session: Session) -> ExperimentDatabaseCounts:
    """Count every synthetic table without including reusable partners."""
    return ExperimentDatabaseCounts(
        delivery_attempts=session.scalar(
            select(func.count()).select_from(DeliveryAttempt)
        )
        or 0,
        delivery_outbox_entries=session.scalar(
            select(func.count()).select_from(DeliveryOutboxEntry)
        )
        or 0,
        events=session.scalar(select(func.count()).select_from(Event)) or 0,
        shipments=session.scalar(select(func.count()).select_from(Shipment)) or 0,
        test_runs=session.scalar(select(func.count()).select_from(ExperimentRunModel))
        or 0,
    )


def _simulator_state(downstream_client: httpx.Client) -> tuple[int, SimulatorMode]:
    status_response = downstream_client.get("/control/status")
    status_response.raise_for_status()
    count_response = downstream_client.get("/control/events/count")
    count_response.raise_for_status()
    try:
        mode = SimulatorMode(status_response.json()["mode"])
        receipt_count = count_response.json()["event_count"]
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentResetError("simulator returned invalid reset state") from error
    if type(receipt_count) is not int or receipt_count < 0:
        raise ExperimentResetError("simulator returned invalid reset state")
    return receipt_count, mode


def inspect_experiment_state(
    *,
    session: Session,
    downstream_client: httpx.Client,
) -> ExperimentStateSnapshot:
    """Read compact application state from RDS and the private simulator."""
    receipts, mode = _simulator_state(downstream_client)
    return ExperimentStateSnapshot(
        database=database_experiment_counts(session),
        simulator_receipts=receipts,
        simulator_mode=mode,
    )


def _clear_database_experiment_state(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text(
                "TRUNCATE TABLE delivery_outbox, delivery_attempts, events, "
                "shipments, test_runs"
            )
        )
    else:
        for model in (
            DeliveryOutboxEntry,
            DeliveryAttempt,
            Event,
            Shipment,
            ExperimentRunModel,
        ):
            session.execute(delete(model))


def reset_experiment_state(
    *,
    session: Session,
    downstream_client: httpx.Client,
    test_run_id: UUID,
    expected_event_count: int,
) -> ExperimentResetEvidence:
    """Reset the sole exact run while preserving infrastructure and partners."""
    before = inspect_experiment_state(
        session=session,
        downstream_client=downstream_client,
    )
    target = session.get(ExperimentRunModel, test_run_id)
    target_event_count = session.scalar(
        select(func.count()).select_from(Event).where(Event.test_run_id == test_run_id)
    )
    if (
        target is None
        or target.expected_event_count != expected_event_count
        or before.database.test_runs != 1
        or before.database.events != expected_event_count
        or before.database.delivery_outbox_entries != expected_event_count
        or before.database.shipments != expected_event_count
        or before.database.delivery_attempts < expected_event_count
        or target_event_count != expected_event_count
        or before.simulator_receipts != expected_event_count
        or before.simulator_mode is not SimulatorMode.HEALTHY
    ):
        raise ExperimentResetError(
            "application state differs from the approved fixed-control run"
        )

    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": SimulatorMode.HEALTHY.value},
    )
    mode_response.raise_for_status()
    _clear_database_experiment_state(session)
    session.commit()
    clear_response = downstream_client.delete("/control/events")
    clear_response.raise_for_status()
    try:
        cleared_receipts = clear_response.json()["cleared_event_count"]
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentResetError(
            "simulator returned invalid reset evidence"
        ) from error
    if type(cleared_receipts) is not int or cleared_receipts < 0:
        raise ExperimentResetError("simulator returned invalid reset evidence")
    after = inspect_experiment_state(
        session=session,
        downstream_client=downstream_client,
    )
    try:
        return ExperimentResetEvidence(
            test_run_id=test_run_id,
            expected_event_count=expected_event_count,
            before=before,
            after=after,
            simulator_receipts_removed=cleared_receipts,
        )
    except ValueError as error:
        raise ExperimentResetError("application reset proof is inconsistent") from error
