"""Compare one generated input manifest with TrackRelay's database."""

from argparse import ArgumentParser
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from trackrelay.config import Settings
from trackrelay.database import session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
)
from trackrelay.experiments.generator import ExpectedEvent, InputManifest
from trackrelay.models import DeliveryAttempt, Event, Shipment
from trackrelay.models import TestRun as ExperimentRunModel

EventIdentity = tuple[str, str]
Count = Annotated[int, Field(ge=0)]


class TestRunNotFoundError(ValueError):
    """Raised when the manifest's run definition is absent from the database."""


class TestRunDefinitionMismatchError(ValueError):
    """Raised when database run metadata disagrees with the input manifest."""


class ReconciliationReport(BaseModel):
    """Basic request and processing accounting for one test run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    generated: Count
    accepted: Count
    rejected: Count
    unique: Count
    processed: Count
    failed: Count
    pending: Count
    unaccounted: Count
    simulator_receipts: Count = 0
    simulator_unique_events: Count = 0
    duplicate_business_effects: Count = 0
    incorrect_final_shipment_states: Count = 0
    invariants_passed: bool = True

    @model_validator(mode="after")
    def require_accounting_invariants(self) -> "ReconciliationReport":
        """Keep request and unique-event totals internally consistent."""
        if self.generated != self.accepted + self.rejected:
            raise ValueError("generated must equal accepted plus rejected")
        if self.unique != self.processed + self.failed + self.pending:
            raise ValueError(
                "unique must equal processed plus failed plus pending"
            )
        if self.unique > self.accepted:
            raise ValueError("unique events cannot exceed accepted requests")
        expected_invariants_passed = (
            self.unique == self.processed + self.failed + self.pending
            and self.unaccounted == 0
            and self.duplicate_business_effects == 0
        )
        if self.invariants_passed is not expected_invariants_passed:
            raise ValueError("invariants_passed disagrees with report counts")
        return self


def load_input_manifest(manifest_path: Path) -> InputManifest:
    """Load and validate a generated JSON input manifest."""
    return InputManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )


def write_reconciliation_report(
    report: ReconciliationReport,
    output_path: Path,
) -> None:
    """Write one stable, human-readable JSON reconciliation report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"{report.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )


def fetch_simulator_receipts(
    downstream_url: str,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = 5.0,
) -> tuple[NormalizedEvent, ...]:
    """Fetch and validate the simulator's current normalized-event receipts."""
    endpoint = f"{downstream_url.rstrip('/')}/events"
    if client is not None:
        response = client.get(endpoint)
    else:
        with httpx.Client(timeout=timeout_seconds) as http_client:
            response = http_client.get(endpoint)
    response.raise_for_status()
    return tuple(TypeAdapter(list[NormalizedEvent]).validate_json(response.content))


def _event_identity(
    event: ExpectedEvent | Event | NormalizedEvent,
) -> EventIdentity:
    return event.partner_id, event.partner_event_id


def _as_utc(value: datetime) -> datetime:
    """Normalize PostgreSQL-aware and SQLite-naive test timestamps."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_matches_expectation(actual: Event, expected: ExpectedEvent) -> bool:
    return (
        actual.tracking_number == expected.tracking_number
        and actual.status is expected.expected_status
        and _as_utc(actual.occurred_at) == _as_utc(expected.expected_occurred_at)
        and actual.raw_payload == expected.payload
    )


def _receipt_matches_expectation(
    receipt: NormalizedEvent,
    expected: ExpectedEvent,
) -> bool:
    return (
        receipt.tracking_number == expected.tracking_number
        and receipt.status is expected.expected_status
        and _as_utc(receipt.occurred_at)
        == _as_utc(expected.expected_occurred_at)
        and receipt.raw_payload == expected.payload
    )


def _require_matching_test_run(
    manifest: InputManifest,
    session: Session,
) -> None:
    test_run = session.get(ExperimentRunModel, manifest.test_run_id)
    if test_run is None:
        raise TestRunNotFoundError(
            f"Test run {manifest.test_run_id} is not present in the database"
        )

    expected_configuration = manifest.configuration.model_dump(mode="json")
    mismatched_fields = []
    if test_run.scenario_name != manifest.scenario_name:
        mismatched_fields.append("scenario_name")
    if test_run.random_seed != manifest.seed:
        mismatched_fields.append("random_seed")
    if test_run.configuration != expected_configuration:
        mismatched_fields.append("configuration")
    if test_run.expected_event_count != manifest.events_generated:
        mismatched_fields.append("expected_event_count")

    if mismatched_fields:
        fields = ", ".join(mismatched_fields)
        raise TestRunDefinitionMismatchError(
            f"Test run definition disagrees with manifest: {fields}"
        )


def reconcile_manifest(
    manifest: InputManifest,
    *,
    session: Session,
    downstream_receipts: Sequence[NormalizedEvent] = (),
) -> ReconciliationReport:
    """Account for manifest inputs, persistence, and downstream effects."""
    _require_matching_test_run(manifest, session)
    database_events = tuple(
        session.scalars(
            select(Event).where(Event.test_run_id == manifest.test_run_id)
        )
    )
    actual_by_identity = {
        _event_identity(event): event for event in database_events
    }
    expected_identities = {
        _event_identity(event) for event in manifest.expected_events
    }

    accepted = sum(
        _event_identity(expected) in actual_by_identity
        for expected in manifest.expected_events
    )
    rejected = manifest.events_generated - accepted
    matched_actual = {
        identity: event
        for identity, event in actual_by_identity.items()
        if identity in expected_identities
    }
    processing_counts = Counter(
        event.processing_status for event in matched_actual.values()
    )

    mismatched_identities: set[EventIdentity] = set()
    for expected in manifest.expected_events:
        identity = _event_identity(expected)
        actual = actual_by_identity.get(identity)
        if actual is not None and not _event_matches_expectation(actual, expected):
            mismatched_identities.add(identity)

    unexpected_identities = set(actual_by_identity) - expected_identities
    unaccounted_identities = mismatched_identities | unexpected_identities

    attempts = tuple(
        session.scalars(
            select(DeliveryAttempt)
            .join(Event, DeliveryAttempt.event_id == Event.id)
            .where(Event.test_run_id == manifest.test_run_id)
        )
    )
    identity_by_event_id = {
        event.id: identity for identity, event in actual_by_identity.items()
    }
    attempt_counts: Counter[EventIdentity] = Counter()
    delivered_attempt_counts: Counter[EventIdentity] = Counter()
    for attempt in attempts:
        identity = identity_by_event_id[attempt.event_id]
        attempt_counts[identity] += 1
        if attempt.result is DeliveryAttemptResult.DELIVERED:
            delivered_attempt_counts[identity] += 1

    run_receipts = tuple(
        receipt
        for receipt in downstream_receipts
        if receipt.test_run_id == manifest.test_run_id
    )
    receipt_counts = Counter(_event_identity(receipt) for receipt in run_receipts)
    expected_by_identity = {
        _event_identity(event): event for event in manifest.expected_events
    }
    for receipt in run_receipts:
        identity = _event_identity(receipt)
        expected = expected_by_identity.get(identity)
        if expected is not None and not _receipt_matches_expectation(
            receipt,
            expected,
        ):
            unaccounted_identities.add(identity)

    for identity, event in matched_actual.items():
        receipt_count = receipt_counts[identity]
        delivered_attempt_count = delivered_attempt_counts[identity]
        if event.processing_status is EventProcessingStatus.PROCESSED:
            if (
                attempt_counts[identity] == 0
                or receipt_count != delivered_attempt_count
            ):
                unaccounted_identities.add(identity)
        elif receipt_count or delivered_attempt_count:
            unaccounted_identities.add(identity)

    receipt_identities = set(receipt_counts)
    unaccounted_identities.update(receipt_identities - set(matched_actual))
    duplicate_business_effects = sum(
        max(0, receipt_count - 1)
        for receipt_count in receipt_counts.values()
    )

    shipments = {
        shipment.tracking_number: shipment
        for shipment in session.scalars(
            select(Shipment).where(
                Shipment.tracking_number.in_(
                    tuple(manifest.expected_final_shipments)
                )
            )
        )
    }
    final_expected_event = {
        tracking_number: max(
            (
                event
                for event in manifest.expected_events
                if event.tracking_number == tracking_number
                and event.expected_status is expected_status
            ),
            key=lambda event: (
                _as_utc(event.expected_occurred_at),
                event.sequence_number,
            ),
        )
        for tracking_number, expected_status in (
            manifest.expected_final_shipments.items()
        )
    }
    incorrect_final_shipment_states = 0
    for tracking_number, expected_status in (
        manifest.expected_final_shipments.items()
    ):
        shipment = shipments.get(tracking_number)
        expected_event = final_expected_event[tracking_number]
        if (
            shipment is None
            or shipment.current_status is not expected_status
            or _as_utc(shipment.current_status_occurred_at)
            != _as_utc(expected_event.expected_occurred_at)
        ):
            incorrect_final_shipment_states += 1

    unaccounted = len(unaccounted_identities)
    invariants_passed = unaccounted == 0 and duplicate_business_effects == 0

    return ReconciliationReport(
        test_run_id=manifest.test_run_id,
        generated=manifest.events_generated,
        accepted=accepted,
        rejected=rejected,
        unique=len(matched_actual),
        processed=processing_counts[EventProcessingStatus.PROCESSED],
        failed=processing_counts[EventProcessingStatus.FAILED],
        pending=processing_counts[EventProcessingStatus.RECEIVED],
        unaccounted=unaccounted,
        simulator_receipts=len(run_receipts),
        simulator_unique_events=len(receipt_counts),
        duplicate_business_effects=duplicate_business_effects,
        incorrect_final_shipment_states=incorrect_final_shipment_states,
        invariants_passed=invariants_passed,
    )


def build_parser() -> ArgumentParser:
    """Describe the basic reconciliation command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--downstream-url", default=Settings().downstream_url)
    return parser


def main() -> None:
    """Reconcile one manifest against the configured TrackRelay database."""
    parser = build_parser()
    arguments = parser.parse_args()
    manifest = load_input_manifest(arguments.manifest)
    try:
        receipts = fetch_simulator_receipts(
            arguments.downstream_url,
            timeout_seconds=Settings().downstream_timeout_seconds,
        )
        with session_factory() as session:
            report = reconcile_manifest(
                manifest,
                session=session,
                downstream_receipts=receipts,
            )
    except (
        TestRunNotFoundError,
        TestRunDefinitionMismatchError,
        httpx.HTTPError,
        ValidationError,
    ) as error:
        parser.error(str(error))

    write_reconciliation_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    print(f"Reconciliation report: {arguments.output}")


if __name__ == "__main__":
    main()
