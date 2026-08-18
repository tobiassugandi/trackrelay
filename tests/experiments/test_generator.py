"""Tests for deterministic experiment input generation."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.domain import ShipmentStatus
from trackrelay.experiments.generator import (
    GeneratorConfiguration,
    InputManifest,
    generate_input_manifest,
    write_input_manifest,
)
from trackrelay.partners import CourierAlphaAdapter, CourierAlphaPayload

CONFIGURATION = GeneratorConfiguration(
    partner_id="alpha-indonesia",
    shipment_count=2,
    start_at=datetime(2026, 8, 6, tzinfo=UTC),
)
RECEIVED_AT = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)


def test_same_seed_and_configuration_generate_identical_manifests() -> None:
    first = generate_input_manifest(seed=20260806, configuration=CONFIGURATION)
    second = generate_input_manifest(seed=20260806, configuration=CONFIGURATION)

    assert first == second
    assert first.model_dump_json(indent=2) == second.model_dump_json(indent=2)


def test_a_different_seed_generates_a_different_dataset() -> None:
    first = generate_input_manifest(seed=1, configuration=CONFIGURATION)
    second = generate_input_manifest(seed=2, configuration=CONFIGURATION)

    assert first.test_run_id != second.test_run_id
    assert first.expected_events != second.expected_events


def test_manifest_contains_sendable_events_and_final_shipment_states() -> None:
    manifest = generate_input_manifest(
        seed=20260806,
        configuration=CONFIGURATION,
    )
    adapter = CourierAlphaAdapter()

    assert manifest.schema_version == 1
    assert manifest.scenario_name == "normal"
    assert manifest.events_generated == 10
    assert manifest.expected_unique_events == 10
    assert [event.sequence_number for event in manifest.expected_events] == list(
        range(1, 11)
    )
    assert all(
        event.test_run_id == manifest.test_run_id
        for event in manifest.expected_events
    )

    for manifest_event in manifest.expected_events:
        payload = CourierAlphaPayload.model_validate(manifest_event.payload)
        normalized = adapter.normalize(
            payload,
            partner_id=manifest_event.partner_id,
            received_at=RECEIVED_AT,
        )
        assert normalized.partner_event_id == manifest_event.partner_event_id
        assert normalized.tracking_number == manifest_event.tracking_number
        assert normalized.status is manifest_event.expected_status
        assert normalized.occurred_at == manifest_event.expected_occurred_at

    assert set(manifest.expected_final_shipments) == {
        event.tracking_number for event in manifest.expected_events
    }
    assert set(manifest.expected_final_shipments.values()) == {
        ShipmentStatus.DELIVERED
    }


def test_explicit_test_run_id_namespaces_every_generated_identifier() -> None:
    test_run_id = UUID("00000000-0000-0000-0000-000000000702")

    manifest = generate_input_manifest(
        seed=20260806,
        configuration=CONFIGURATION,
        test_run_id=test_run_id,
    )

    assert manifest.test_run_id == test_run_id
    assert all(
        test_run_id.hex in event.partner_event_id
        and test_run_id.hex in event.tracking_number
        for event in manifest.expected_events
    )


def test_written_manifest_round_trips_through_the_validated_schema(
    tmp_path: Path,
) -> None:
    manifest = generate_input_manifest(
        seed=20260806,
        configuration=CONFIGURATION,
    )
    output_path = tmp_path / "run" / "input-manifest.json"

    write_input_manifest(manifest, output_path)

    written = output_path.read_text(encoding="utf-8")
    assert written.endswith("\n")
    assert InputManifest.model_validate_json(written) == manifest


def test_manifest_rejects_a_summary_that_does_not_match_its_events() -> None:
    manifest = generate_input_manifest(
        seed=20260806,
        configuration=CONFIGURATION,
    )
    invalid = json.loads(manifest.model_dump_json())
    invalid["events_generated"] += 1

    with raises(ValidationError, match="events_generated"):
        InputManifest.model_validate(invalid)


def test_manifest_rejects_a_final_state_missing_from_shipment_history() -> None:
    configuration = GeneratorConfiguration(
        partner_id="alpha-indonesia",
        shipment_count=1,
    )
    manifest = generate_input_manifest(
        seed=20260806,
        configuration=configuration,
    )
    invalid = json.loads(manifest.model_dump_json())
    invalid["expected_events"] = invalid["expected_events"][:-1]
    invalid["events_generated"] = 4
    invalid["expected_unique_events"] = 4

    with raises(ValidationError, match="final states missing"):
        InputManifest.model_validate(invalid)


def test_generator_advances_each_shipment_through_the_five_statuses() -> None:
    one_shipment = GeneratorConfiguration(
        partner_id="alpha-indonesia",
        shipment_count=1,
        start_at=CONFIGURATION.start_at + timedelta(days=1),
    )

    manifest = generate_input_manifest(seed=9, configuration=one_shipment)

    assert [event.expected_status for event in manifest.expected_events] == list(
        ShipmentStatus
    )
