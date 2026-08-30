"""Tests for the intentionally small Stage 9.3 result."""

from datetime import UTC, datetime

from pydantic import ValidationError
from pytest import raises

from trackrelay.aws_flexibility_report import (
    HardwareFlexibilityReport,
    ShortTierResult,
    _render_markdown,
)


def tier(instance_type: str, maximum: int | None, failure: int | None):
    return ShortTierResult(
        instance_type=instance_type,
        tested_rates_per_second=(10, 25, 50),
        maximum_passing_rate_per_second=maximum,
        first_failing_rate_per_second=failure,
    )


def test_short_report_states_the_observed_improvement_plainly() -> None:
    report = HardwareFlexibilityReport(
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        source=tier("t3.small", 25, 50),
        target=tier("c7i-flex.large", 50, None),
        stronger_machine_passed_a_higher_rate=True,
        conclusion="The stronger machine passed a higher rate.",
    )

    markdown = _render_markdown(report)

    assert "`t3.small`" in markdown
    assert "`c7i-flex.large`" in markdown
    assert "25/s" in markdown
    assert "50/s" in markdown
    assert "not a long-duration capacity estimate" in markdown


def test_short_report_rejects_an_unearned_improvement_claim() -> None:
    with raises(ValidationError, match="improvement flag"):
        HardwareFlexibilityReport(
            generated_at=datetime(2026, 8, 31, tzinfo=UTC),
            source=tier("t3.small", 50, None),
            target=tier("c7i-flex.large", 50, None),
            stronger_machine_passed_a_higher_rate=True,
            conclusion="Incorrect claim.",
        )
