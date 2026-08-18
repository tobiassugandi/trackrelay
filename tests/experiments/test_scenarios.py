"""Tests for deterministic correctness-scenario manifests."""

from uuid import UUID

from trackrelay.domain import ShipmentStatus
from trackrelay.experiments.generator import GeneratorConfiguration
from trackrelay.experiments.scenarios import (
    CorrectnessScenario,
    build_correctness_manifest,
)

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000801")
CONFIGURATION = GeneratorConfiguration(
    partner_id="scenario-alpha",
    shipment_count=1,
)


def build_manifest(scenario: CorrectnessScenario):
    return build_correctness_manifest(
        scenario,
        seed=20260818,
        configuration=CONFIGURATION,
        test_run_id=TEST_RUN_ID,
    )


def test_normal_scenario_sends_one_ordered_shipment_history() -> None:
    manifest = build_manifest(CorrectnessScenario.NORMAL)

    assert manifest.scenario_name == "normal"
    assert manifest.events_generated == 5
    assert manifest.expected_unique_events == 5
    assert [event.expected_status for event in manifest.expected_events] == list(
        ShipmentStatus
    )


def test_duplicate_scenario_replays_the_same_business_identities() -> None:
    manifest = build_manifest(CorrectnessScenario.DUPLICATE)

    first_delivery = manifest.expected_events[:5]
    duplicate_delivery = manifest.expected_events[5:]
    assert manifest.scenario_name == "duplicate"
    assert manifest.events_generated == 10
    assert manifest.expected_unique_events == 5
    assert [
        (event.partner_id, event.partner_event_id)
        for event in duplicate_delivery
    ] == [
        (event.partner_id, event.partner_event_id)
        for event in first_delivery
    ]


def test_out_of_order_scenario_sends_each_history_in_reverse() -> None:
    manifest = build_manifest(CorrectnessScenario.OUT_OF_ORDER)

    assert manifest.scenario_name == "out-of-order"
    assert manifest.events_generated == 5
    assert manifest.expected_unique_events == 5
    assert [event.expected_status for event in manifest.expected_events] == list(
        reversed(tuple(ShipmentStatus))
    )
    assert tuple(
        event.sequence_number for event in manifest.expected_events
    ) == tuple(range(1, 6))


def test_downstream_outage_uses_the_normal_manifest_history() -> None:
    manifest = build_manifest(CorrectnessScenario.DOWNSTREAM_OUTAGE)

    assert manifest.scenario_name == "downstream-outage"
    assert manifest.events_generated == 5
    assert manifest.expected_unique_events == 5
    assert [event.expected_status for event in manifest.expected_events] == list(
        ShipmentStatus
    )
