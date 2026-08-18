"""Reconcile manifest, database, and downstream evidence for one test run.

The test-run ID scopes an experiment. Within that run, the partner ID plus
partner event ID is the shared business identity used to match manifest events,
database events, delivery attempts, and simulator receipts. Tracking numbers
group those events into shipment histories for the final-state comparison.
"""

from argparse import ArgumentParser
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
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
from trackrelay.experiments.generator import InputManifest, ManifestEvent
from trackrelay.models import DeliveryAttempt, Event, Shipment
from trackrelay.models import TestRun as ExperimentRunModel

BusinessEventIdentity = tuple[str, str]
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
        invariants_implied_by_counts = (
            self.unique == self.processed + self.failed + self.pending
            and self.unaccounted == 0
            and self.duplicate_business_effects == 0
        )
        if self.invariants_passed is not invariants_implied_by_counts:
            raise ValueError("invariants_passed disagrees with report counts")
        return self


@dataclass(frozen=True)
class DatabaseEventComparison:
    """Result of comparing manifest events with persisted database events."""

    accepted_manifest_request_count: int
    rejected_manifest_request_count: int
    database_event_by_business_identity: dict[BusinessEventIdentity, Event]
    database_event_matching_manifest_by_identity: dict[
        BusinessEventIdentity,
        Event,
    ]
    database_processing_status_count: Counter[EventProcessingStatus]
    database_content_mismatch_identities: set[BusinessEventIdentity]
    unexpected_database_event_identities: set[BusinessEventIdentity]


@dataclass(frozen=True)
class DownstreamDeliveryComparison:
    """Result of comparing delivery attempts with simulator receipts."""

    simulator_receipt_count: int
    simulator_unique_event_count: int
    duplicate_business_effect_count: int
    simulator_content_mismatch_identities: set[BusinessEventIdentity]
    unexpected_simulator_receipt_identities: set[BusinessEventIdentity]
    processed_without_delivery_attempt_identities: set[
        BusinessEventIdentity
    ]
    delivery_evidence_mismatch_identities: set[BusinessEventIdentity]


@dataclass(frozen=True)
class FinalShipmentComparison:
    """Result of comparing manifest final states with database shipments."""

    incorrect_database_shipment_count: int


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


def _business_event_identity(
    event: ManifestEvent | Event | NormalizedEvent,
) -> BusinessEventIdentity:
    return event.partner_id, event.partner_event_id


def _as_utc(value: datetime) -> datetime:
    """Normalize PostgreSQL-aware and SQLite-naive test timestamps."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_event_matches_manifest_event(
    database_event: Event,
    manifest_event: ManifestEvent,
) -> bool:
    return (
        database_event.tracking_number == manifest_event.tracking_number
        and database_event.status is manifest_event.expected_status
        and _as_utc(database_event.occurred_at)
        == _as_utc(manifest_event.expected_occurred_at)
        and database_event.raw_payload == manifest_event.payload
    )


def _simulator_receipt_matches_manifest_event(
    simulator_receipt: NormalizedEvent,
    manifest_event: ManifestEvent,
) -> bool:
    return (
        simulator_receipt.tracking_number == manifest_event.tracking_number
        and simulator_receipt.status is manifest_event.expected_status
        and _as_utc(simulator_receipt.occurred_at)
        == _as_utc(manifest_event.expected_occurred_at)
        and simulator_receipt.raw_payload == manifest_event.payload
    )


def _require_database_test_run_matches_manifest(
    manifest: InputManifest,
    session: Session,
) -> None:
    database_test_run = session.get(ExperimentRunModel, manifest.test_run_id)
    if database_test_run is None:
        raise TestRunNotFoundError(
            f"Test run {manifest.test_run_id} is not present in the database"
        )

    manifest_configuration = manifest.configuration.model_dump(mode="json")
    definition_mismatch_fields = []
    if database_test_run.scenario_name != manifest.scenario_name:
        definition_mismatch_fields.append("scenario_name")
    if database_test_run.random_seed != manifest.seed:
        definition_mismatch_fields.append("random_seed")
    if database_test_run.configuration != manifest_configuration:
        definition_mismatch_fields.append("configuration")
    if database_test_run.expected_event_count != manifest.events_generated:
        definition_mismatch_fields.append("expected_event_count")

    if definition_mismatch_fields:
        mismatch_field_names = ", ".join(definition_mismatch_fields)
        raise TestRunDefinitionMismatchError(
            "Test run definition disagrees with manifest: "
            f"{mismatch_field_names}"
        )


def _compare_manifest_with_database_events(
    manifest: InputManifest,
    *,
    database_events: Sequence[Event],
) -> DatabaseEventComparison:
    manifest_event_by_identity = {
        _business_event_identity(manifest_event): manifest_event
        for manifest_event in manifest.expected_events
    }
    database_event_by_identity = {
        _business_event_identity(database_event): database_event
        for database_event in database_events
    }

    manifest_event_identities = set(manifest_event_by_identity)
    database_event_matching_manifest_by_identity = {
        identity: database_event
        for identity, database_event in database_event_by_identity.items()
        if identity in manifest_event_identities
    }
    accepted_manifest_request_count = sum(
        _business_event_identity(manifest_event) in database_event_by_identity
        for manifest_event in manifest.expected_events
    )

    database_content_mismatch_identities: set[BusinessEventIdentity] = set()
    for identity, manifest_event in manifest_event_by_identity.items():
        database_event = database_event_by_identity.get(identity)
        if database_event is None:
            continue
        if not _database_event_matches_manifest_event(
            database_event,
            manifest_event,
        ):
            database_content_mismatch_identities.add(identity)
    unexpected_database_event_identities = (
        set(database_event_by_identity) - manifest_event_identities
    )

    return DatabaseEventComparison(
        accepted_manifest_request_count=accepted_manifest_request_count,
        rejected_manifest_request_count=(
            manifest.events_generated - accepted_manifest_request_count
        ),
        database_event_by_business_identity=database_event_by_identity,
        database_event_matching_manifest_by_identity=(
            database_event_matching_manifest_by_identity
        ),
        database_processing_status_count=Counter(
            database_event.processing_status
            for database_event in (
                database_event_matching_manifest_by_identity.values()
            )
        ),
        database_content_mismatch_identities=(
            database_content_mismatch_identities
        ),
        unexpected_database_event_identities=(
            unexpected_database_event_identities
        ),
    )


def _compare_delivery_attempts_with_simulator_receipts(
    manifest: InputManifest,
    *,
    database_event_by_business_identity: dict[BusinessEventIdentity, Event],
    database_event_matching_manifest_by_identity: dict[
        BusinessEventIdentity,
        Event,
    ],
    database_delivery_attempts: Sequence[DeliveryAttempt],
    simulator_receipts_for_run: Sequence[NormalizedEvent],
) -> DownstreamDeliveryComparison:
    manifest_event_by_identity = {
        _business_event_identity(manifest_event): manifest_event
        for manifest_event in manifest.expected_events
    }
    business_identity_by_database_event_id = {
        database_event.id: identity
        for identity, database_event in (
            database_event_by_business_identity.items()
        )
    }

    delivery_attempt_count_by_identity: Counter[BusinessEventIdentity] = Counter()
    successful_attempt_count_by_identity: Counter[BusinessEventIdentity] = Counter()
    for database_delivery_attempt in database_delivery_attempts:
        identity = business_identity_by_database_event_id[
            database_delivery_attempt.event_id
        ]
        delivery_attempt_count_by_identity[identity] += 1
        if database_delivery_attempt.result is DeliveryAttemptResult.DELIVERED:
            successful_attempt_count_by_identity[identity] += 1

    simulator_receipt_count_by_identity = Counter(
        _business_event_identity(simulator_receipt)
        for simulator_receipt in simulator_receipts_for_run
    )
    simulator_content_mismatch_identities: set[BusinessEventIdentity] = set()
    for simulator_receipt in simulator_receipts_for_run:
        identity = _business_event_identity(simulator_receipt)
        manifest_event = manifest_event_by_identity.get(identity)
        if manifest_event is None:
            continue
        if not _simulator_receipt_matches_manifest_event(
            simulator_receipt,
            manifest_event,
        ):
            simulator_content_mismatch_identities.add(identity)
    unexpected_simulator_receipt_identities = set(
        simulator_receipt_count_by_identity
    ) - set(database_event_matching_manifest_by_identity)

    processed_without_delivery_attempt_identities: set[
        BusinessEventIdentity
    ] = set()
    delivery_evidence_mismatch_identities: set[BusinessEventIdentity] = set()
    database_events_matching_manifest = (
        database_event_matching_manifest_by_identity.items()
    )
    for identity, database_event in database_events_matching_manifest:
        delivery_attempt_count = delivery_attempt_count_by_identity[identity]
        successful_attempt_count = successful_attempt_count_by_identity[identity]
        simulator_receipt_count = simulator_receipt_count_by_identity[identity]

        if database_event.processing_status is EventProcessingStatus.PROCESSED:
            if delivery_attempt_count == 0:
                processed_without_delivery_attempt_identities.add(identity)
            if simulator_receipt_count != successful_attempt_count:
                delivery_evidence_mismatch_identities.add(identity)
        elif simulator_receipt_count or successful_attempt_count:
            delivery_evidence_mismatch_identities.add(identity)

    duplicate_business_effect_count = sum(
        max(0, simulator_receipt_count - 1)
        for simulator_receipt_count in (
            simulator_receipt_count_by_identity.values()
        )
    )
    return DownstreamDeliveryComparison(
        simulator_receipt_count=len(simulator_receipts_for_run),
        simulator_unique_event_count=len(simulator_receipt_count_by_identity),
        duplicate_business_effect_count=duplicate_business_effect_count,
        simulator_content_mismatch_identities=(
            simulator_content_mismatch_identities
        ),
        unexpected_simulator_receipt_identities=(
            unexpected_simulator_receipt_identities
        ),
        processed_without_delivery_attempt_identities=(
            processed_without_delivery_attempt_identities
        ),
        delivery_evidence_mismatch_identities=(
            delivery_evidence_mismatch_identities
        ),
    )


def _compare_manifest_with_database_shipments(
    manifest: InputManifest,
    *,
    database_shipments: Sequence[Shipment],
) -> FinalShipmentComparison:
    database_shipment_by_tracking_number = {
        database_shipment.tracking_number: database_shipment
        for database_shipment in database_shipments
    }
    manifest_final_event_by_tracking_number = {
        tracking_number: max(
            (
                manifest_event
                for manifest_event in manifest.expected_events
                if manifest_event.tracking_number == tracking_number
                and manifest_event.expected_status is manifest_final_status
            ),
            key=lambda manifest_event: (
                _as_utc(manifest_event.expected_occurred_at),
                manifest_event.sequence_number,
            ),
        )
        for tracking_number, manifest_final_status in (
            manifest.expected_final_shipments.items()
        )
    }

    incorrect_database_shipment_count = 0
    for tracking_number, manifest_final_status in (
        manifest.expected_final_shipments.items()
    ):
        database_shipment = database_shipment_by_tracking_number.get(
            tracking_number
        )
        manifest_final_event = manifest_final_event_by_tracking_number[
            tracking_number
        ]
        if (
            database_shipment is None
            or database_shipment.current_status is not manifest_final_status
            or _as_utc(database_shipment.current_status_occurred_at)
            != _as_utc(manifest_final_event.expected_occurred_at)
        ):
            incorrect_database_shipment_count += 1

    return FinalShipmentComparison(
        incorrect_database_shipment_count=incorrect_database_shipment_count
    )


def reconcile_manifest(
    manifest: InputManifest,
    *,
    session: Session,
    simulator_receipts: Sequence[NormalizedEvent] = (),
) -> ReconciliationReport:
    """Tell the reconciliation story one evidence-source boundary at a time."""
    _require_database_test_run_matches_manifest(manifest, session)

    database_events = tuple(
        session.scalars(
            select(Event).where(Event.test_run_id == manifest.test_run_id)
        )
    )
    database_event_comparison = _compare_manifest_with_database_events(
        manifest,
        database_events=database_events,
    )

    database_delivery_attempts = tuple(
        session.scalars(
            select(DeliveryAttempt)
            .join(Event, DeliveryAttempt.event_id == Event.id)
            .where(Event.test_run_id == manifest.test_run_id)
        )
    )
    simulator_receipts_for_run = tuple(
        simulator_receipt
        for simulator_receipt in simulator_receipts
        if simulator_receipt.test_run_id == manifest.test_run_id
    )
    downstream_delivery_comparison = _compare_delivery_attempts_with_simulator_receipts(
        manifest,
        database_event_by_business_identity=(
            database_event_comparison.database_event_by_business_identity
        ),
        database_event_matching_manifest_by_identity=(
            database_event_comparison.database_event_matching_manifest_by_identity
        ),
        database_delivery_attempts=database_delivery_attempts,
        simulator_receipts_for_run=simulator_receipts_for_run,
    )

    database_shipments = tuple(
        session.scalars(
            select(Shipment).where(
                Shipment.tracking_number.in_(
                    tuple(manifest.expected_final_shipments)
                )
            )
        )
    )
    final_shipment_comparison = _compare_manifest_with_database_shipments(
        manifest,
        database_shipments=database_shipments,
    )

    database_content_mismatches = (
        database_event_comparison.database_content_mismatch_identities
    )
    unexpected_database_events = (
        database_event_comparison.unexpected_database_event_identities
    )
    simulator_content_mismatches = (
        downstream_delivery_comparison.simulator_content_mismatch_identities
    )
    unexpected_simulator_receipts = (
        downstream_delivery_comparison.unexpected_simulator_receipt_identities
    )
    processed_without_delivery_attempts = (
        downstream_delivery_comparison.processed_without_delivery_attempt_identities
    )
    delivery_evidence_mismatches = (
        downstream_delivery_comparison.delivery_evidence_mismatch_identities
    )
    unaccounted_business_identities = (
        database_content_mismatches
        | unexpected_database_events
        | simulator_content_mismatches
        | unexpected_simulator_receipts
        | processed_without_delivery_attempts
        | delivery_evidence_mismatches
    )
    unaccounted_count = len(unaccounted_business_identities)
    invariants_passed = (
        unaccounted_count == 0
        and downstream_delivery_comparison.duplicate_business_effect_count == 0
    )

    return ReconciliationReport(
        test_run_id=manifest.test_run_id,
        generated=manifest.events_generated,
        accepted=database_event_comparison.accepted_manifest_request_count,
        rejected=database_event_comparison.rejected_manifest_request_count,
        unique=len(
            database_event_comparison.database_event_matching_manifest_by_identity
        ),
        processed=database_event_comparison.database_processing_status_count[
            EventProcessingStatus.PROCESSED
        ],
        failed=database_event_comparison.database_processing_status_count[
            EventProcessingStatus.FAILED
        ],
        pending=database_event_comparison.database_processing_status_count[
            EventProcessingStatus.RECEIVED
        ],
        unaccounted=unaccounted_count,
        simulator_receipts=(
            downstream_delivery_comparison.simulator_receipt_count
        ),
        simulator_unique_events=(
            downstream_delivery_comparison.simulator_unique_event_count
        ),
        duplicate_business_effects=(
            downstream_delivery_comparison.duplicate_business_effect_count
        ),
        incorrect_final_shipment_states=(
            final_shipment_comparison.incorrect_database_shipment_count
        ),
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
        simulator_receipts = fetch_simulator_receipts(
            arguments.downstream_url,
            timeout_seconds=Settings().downstream_timeout_seconds,
        )
        with session_factory() as session:
            report = reconcile_manifest(
                manifest,
                session=session,
                simulator_receipts=simulator_receipts,
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
