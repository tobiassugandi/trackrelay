"""Compare one generated input manifest with TrackRelay's database."""

from argparse import ArgumentParser
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trackrelay.database import session_factory
from trackrelay.domain import EventProcessingStatus
from trackrelay.experiments.generator import ExpectedEvent, InputManifest
from trackrelay.models import Event
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


def _event_identity(event: ExpectedEvent | Event) -> EventIdentity:
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
) -> ReconciliationReport:
    """Account for manifest requests and unique persisted run events."""
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
    unaccounted = len(mismatched_identities | unexpected_identities)

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
    )


def build_parser() -> ArgumentParser:
    """Describe the basic reconciliation command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> None:
    """Reconcile one manifest against the configured TrackRelay database."""
    parser = build_parser()
    arguments = parser.parse_args()
    manifest = load_input_manifest(arguments.manifest)
    try:
        with session_factory() as session:
            report = reconcile_manifest(manifest, session=session)
    except (TestRunNotFoundError, TestRunDefinitionMismatchError) as error:
        parser.error(str(error))

    write_reconciliation_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    print(f"Reconciliation report: {arguments.output}")


if __name__ == "__main__":
    main()
