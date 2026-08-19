"""Define and evaluate the initial local experiment SLO."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from trackrelay.experiments.reconciliation import ReconciliationReport

DurationMilliseconds = Annotated[float, Field(ge=0)]
Rate = Annotated[float, Field(ge=0, le=1)]
Count = Annotated[int, Field(ge=0)]


class BaselineSLODefinition(BaseModel):
    """The deliberately provisional success targets for local experiments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["initial-local-baseline-v1"] = "initial-local-baseline-v1"
    scope: Literal["local_experiment"] = "local_experiment"
    p95_response_latency_must_be_below_ms: DurationMilliseconds = 500
    request_error_rate_must_be_below: Rate = 0.01
    unaccounted_events_must_equal: Count = 0


INITIAL_BASELINE_SLO = BaselineSLODefinition()


class BaselineSLOEvaluation(BaseModel):
    """Observed values and their unambiguous pass/fail interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: BaselineSLODefinition
    p95_response_latency_ms: DurationMilliseconds
    request_error_rate: Rate
    unaccounted_events: Count
    reconciliation_invariants_passed: bool

    @computed_field
    @property
    def latency_target_passed(self) -> bool:
        return (
            self.p95_response_latency_ms
            < self.definition.p95_response_latency_must_be_below_ms
        )

    @computed_field
    @property
    def error_rate_target_passed(self) -> bool:
        return (
            self.request_error_rate
            < self.definition.request_error_rate_must_be_below
        )

    @computed_field
    @property
    def accounting_target_passed(self) -> bool:
        return (
            self.unaccounted_events
            == self.definition.unaccounted_events_must_equal
        )

    @computed_field
    @property
    def slo_passed(self) -> bool:
        """Whether the three explicitly defined SLO targets passed."""
        return (
            self.latency_target_passed
            and self.error_rate_target_passed
            and self.accounting_target_passed
        )

    @computed_field
    @property
    def experiment_passed(self) -> bool:
        """Whether the SLO and every reconciliation invariant passed."""
        return self.slo_passed and self.reconciliation_invariants_passed


def evaluate_baseline_slo(
    *,
    p95_response_latency_ms: float,
    request_error_rate: float,
    reconciliation_report: ReconciliationReport,
    definition: BaselineSLODefinition = INITIAL_BASELINE_SLO,
) -> BaselineSLOEvaluation:
    """Evaluate observed k6 metrics and the matching reconciliation report."""
    return BaselineSLOEvaluation(
        definition=definition,
        p95_response_latency_ms=p95_response_latency_ms,
        request_error_rate=request_error_rate,
        unaccounted_events=reconciliation_report.unaccounted,
        reconciliation_invariants_passed=(
            reconciliation_report.invariants_passed
        ),
    )
