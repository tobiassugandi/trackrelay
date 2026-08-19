"""Tests for the initial local experiment SLO."""

from uuid import UUID

from pytest import mark

from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.slo import (
    INITIAL_BASELINE_SLO,
    evaluate_baseline_slo,
)

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000803")


def reconciliation_report(
    *,
    unaccounted: int = 0,
    duplicate_business_effects: int = 0,
    incorrect_final_shipment_states: int = 0,
) -> ReconciliationReport:
    invariants_passed = (
        unaccounted == 0
        and duplicate_business_effects == 0
        and incorrect_final_shipment_states == 0
    )
    return ReconciliationReport(
        test_run_id=TEST_RUN_ID,
        generated=1,
        accepted=1,
        rejected=0,
        unique=1,
        processed=1,
        failed=0,
        pending=0,
        unaccounted=unaccounted,
        duplicate_business_effects=duplicate_business_effects,
        incorrect_final_shipment_states=incorrect_final_shipment_states,
        invariants_passed=invariants_passed,
    )


def test_initial_baseline_slo_is_an_explicit_local_experiment_definition() -> None:
    assert INITIAL_BASELINE_SLO.model_dump() == {
        "schema_version": 1,
        "name": "initial-local-baseline-v1",
        "scope": "local_experiment",
        "p95_response_latency_must_be_below_ms": 500.0,
        "request_error_rate_must_be_below": 0.01,
        "unaccounted_events_must_equal": 0,
    }


def test_evaluation_passes_below_both_limits_with_clean_reconciliation() -> None:
    evaluation = evaluate_baseline_slo(
        p95_response_latency_ms=499.9,
        request_error_rate=0.009,
        reconciliation_report=reconciliation_report(),
    )

    assert evaluation.latency_target_passed is True
    assert evaluation.error_rate_target_passed is True
    assert evaluation.accounting_target_passed is True
    assert evaluation.slo_passed is True
    assert evaluation.reconciliation_invariants_passed is True
    assert evaluation.experiment_passed is True


@mark.parametrize(
    ("p95_response_latency_ms", "request_error_rate"),
    ((500.0, 0.0), (0.0, 0.01)),
)
def test_limits_are_exclusive(
    p95_response_latency_ms: float,
    request_error_rate: float,
) -> None:
    evaluation = evaluate_baseline_slo(
        p95_response_latency_ms=p95_response_latency_ms,
        request_error_rate=request_error_rate,
        reconciliation_report=reconciliation_report(),
    )

    assert evaluation.slo_passed is False
    assert evaluation.experiment_passed is False


def test_unaccounted_event_fails_the_accounting_target() -> None:
    evaluation = evaluate_baseline_slo(
        p95_response_latency_ms=100,
        request_error_rate=0,
        reconciliation_report=reconciliation_report(unaccounted=1),
    )

    assert evaluation.accounting_target_passed is False
    assert evaluation.slo_passed is False
    assert evaluation.experiment_passed is False


def test_complete_experiment_requires_every_reconciliation_invariant() -> None:
    evaluation = evaluate_baseline_slo(
        p95_response_latency_ms=100,
        request_error_rate=0,
        reconciliation_report=reconciliation_report(
            incorrect_final_shipment_states=1
        ),
    )

    assert evaluation.slo_passed is True
    assert evaluation.reconciliation_invariants_passed is False
    assert evaluation.experiment_passed is False
