"""Deterministically generate a normal Courier Alpha input manifest."""

from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import AlphaStatusCode, CourierAlphaPayload

DEFAULT_START_AT = datetime(2026, 8, 6, tzinfo=UTC)
NORMAL_STATUSES = tuple(ShipmentStatus)
ALPHA_STATUS_FOR = {
    ShipmentStatus.CREATED: AlphaStatusCode.CREATED,
    ShipmentStatus.PICKED_UP: AlphaStatusCode.PICKED_UP,
    ShipmentStatus.IN_TRANSIT: AlphaStatusCode.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY: AlphaStatusCode.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED: AlphaStatusCode.DELIVERED,
}
ManifestIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
ScenarioName = Literal[
    "normal",
    "duplicate",
    "out-of-order",
    "downstream-outage",
    "healthy-baseline",
    "slow-under-load",
    "outage-under-load",
    "elasticity-fixed-control",
    "elasticity-elastic-treatment",
]


class GeneratorConfiguration(BaseModel):
    """Inputs that define one deterministic normal dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    partner_id: ManifestIdentifier = "alpha-indonesia"
    shipment_count: Annotated[int, Field(gt=0)]
    start_at: AwareDatetime = DEFAULT_START_AT


class ManifestEvent(BaseModel):
    """One sendable request and its manifest-declared normalized fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_number: Annotated[int, Field(gt=0)]
    test_run_id: UUID
    partner_id: ManifestIdentifier
    partner_event_id: ManifestIdentifier
    tracking_number: ManifestIdentifier
    expected_status: ShipmentStatus
    expected_occurred_at: AwareDatetime
    payload: dict[str, JsonValue]


class InputManifest(BaseModel):
    """The immutable inputs and declared outcomes for one experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    scenario_name: ScenarioName = "normal"
    seed: int
    configuration: GeneratorConfiguration
    events_generated: Annotated[int, Field(ge=0)]
    expected_unique_events: Annotated[int, Field(ge=0)]
    expected_events: tuple[ManifestEvent, ...]
    expected_final_shipments: dict[str, ShipmentStatus]

    @model_validator(mode="after")
    def require_consistent_expectations(self) -> "InputManifest":
        """Reject manifests whose summary disagrees with their event details."""
        if self.events_generated != len(self.expected_events):
            raise ValueError("events_generated must match expected_events")

        unique_event_ids = {
            (event.partner_id, event.partner_event_id)
            for event in self.expected_events
        }
        if self.expected_unique_events != len(unique_event_ids):
            raise ValueError(
                "expected_unique_events must match unique partner event IDs"
            )

        required_sequence = tuple(range(1, self.events_generated + 1))
        manifest_sequence = tuple(
            event.sequence_number for event in self.expected_events
        )
        if manifest_sequence != required_sequence:
            raise ValueError("expected event sequence must be contiguous")

        if any(
            event.test_run_id != self.test_run_id
            for event in self.expected_events
        ):
            raise ValueError("every expected event must use the manifest test run ID")

        manifest_tracking_numbers = {
            event.tracking_number for event in self.expected_events
        }
        if manifest_tracking_numbers != set(self.expected_final_shipments):
            raise ValueError(
                "expected final shipments must match generated tracking numbers"
            )
        historical_states = {
            (event.tracking_number, event.expected_status)
            for event in self.expected_events
        }
        manifest_final_states = set(self.expected_final_shipments.items())
        missing_final_states = manifest_final_states - historical_states
        if missing_final_states:
            raise ValueError(
                "final states missing from shipment histories: "
                f"{missing_final_states}"
            )
        return self


def derive_test_run_id(*, seed: int, configuration: GeneratorConfiguration) -> UUID:
    """Derive a stable run UUID from every input that shapes the dataset."""
    identity = ":".join(
        (
            "trackrelay",
            "normal",
            configuration.partner_id,
            str(seed),
            str(configuration.shipment_count),
            configuration.start_at.isoformat(),
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def generate_input_manifest(
    *,
    seed: int,
    configuration: GeneratorConfiguration,
    test_run_id: UUID | None = None,
) -> InputManifest:
    """Generate the same event histories for the same complete set of inputs."""
    resolved_test_run_id = test_run_id or derive_test_run_id(
        seed=seed,
        configuration=configuration,
    )
    random = Random(seed)
    manifest_events: list[ManifestEvent] = []
    manifest_final_shipment_statuses: dict[str, ShipmentStatus] = {}
    sequence_number = 1

    for shipment_number in range(1, configuration.shipment_count + 1):
        random_suffix = random.getrandbits(32)
        tracking_number = (
            f"SYN-{resolved_test_run_id.hex}-{shipment_number:06d}-"
            f"{random_suffix:08x}"
        )
        shipment_start = configuration.start_at + timedelta(
            minutes=(shipment_number - 1) * len(NORMAL_STATUSES),
            seconds=random.randrange(60),
        )

        for status_offset, normalized_status in enumerate(NORMAL_STATUSES):
            occurred_at = shipment_start + timedelta(minutes=status_offset)
            partner_event_id = (
                f"SYN-{resolved_test_run_id.hex}-{sequence_number:08d}"
            )
            payload = CourierAlphaPayload(
                event_id=partner_event_id,
                tracking_number=tracking_number,
                status=ALPHA_STATUS_FOR[normalized_status],
                event_time=occurred_at,
            ).model_dump(mode="json")
            manifest_events.append(
                ManifestEvent(
                    sequence_number=sequence_number,
                    test_run_id=resolved_test_run_id,
                    partner_id=configuration.partner_id,
                    partner_event_id=partner_event_id,
                    tracking_number=tracking_number,
                    expected_status=normalized_status,
                    expected_occurred_at=occurred_at,
                    payload=payload,
                )
            )
            sequence_number += 1

        manifest_final_shipment_statuses[tracking_number] = (
            ShipmentStatus.DELIVERED
        )

    event_count = len(manifest_events)
    return InputManifest(
        test_run_id=resolved_test_run_id,
        seed=seed,
        configuration=configuration,
        events_generated=event_count,
        expected_unique_events=event_count,
        expected_events=tuple(manifest_events),
        expected_final_shipments=manifest_final_shipment_statuses,
    )


def write_input_manifest(manifest: InputManifest, output_path: Path) -> None:
    """Write one stable, human-readable JSON manifest."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"{manifest.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )


def build_parser() -> ArgumentParser:
    """Describe the deterministic generator command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--shipments", required=True, type=int)
    parser.add_argument("--partner-id", default="alpha-indonesia")
    parser.add_argument("--start-at", default=DEFAULT_START_AT.isoformat())
    parser.add_argument("--test-run-id", type=UUID)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> None:
    """Generate and save one input manifest from command-line arguments."""
    arguments = build_parser().parse_args()
    configuration = GeneratorConfiguration(
        partner_id=arguments.partner_id,
        shipment_count=arguments.shipments,
        start_at=arguments.start_at,
    )
    manifest = generate_input_manifest(
        seed=arguments.seed,
        configuration=configuration,
        test_run_id=arguments.test_run_id,
    )
    write_input_manifest(manifest, arguments.output)
    print(f"Input manifest: {arguments.output}")
    print(f"Test run ID: {manifest.test_run_id}")
    print(f"Expected events: {manifest.events_generated}")


if __name__ == "__main__":
    main()
