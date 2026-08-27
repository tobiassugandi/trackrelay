"""Run the complete correctness suite inside the private RDS deployment."""

from argparse import ArgumentParser
from base64 import b64encode
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID, uuid5

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from trackrelay.config import Settings
from trackrelay.experiments.generator import (
    DEFAULT_START_AT,
    GeneratorConfiguration,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.scenarios import (
    CorrectnessScenario,
    ScenarioObservations,
    execute_correctness_scenario,
)

PositiveInteger = Annotated[int, Field(gt=0)]
RDS_CORRECTNESS_EVIDENCE_PREFIX = "TRACKRELAY_RDS_CORRECTNESS_EVIDENCE="
CORE_CORRECTNESS_SCENARIOS = tuple(CorrectnessScenario)


class RdsCorrectnessDefinition(BaseModel):
    """Stable identity and inputs for one four-scenario RDS suite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    suite_id: UUID
    random_seed: int = 20260806
    shipment_count: PositiveInteger = 1
    partner_id: str = Field(
        default="rds-correctness-alpha",
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    start_at: AwareDatetime = DEFAULT_START_AT
    scenarios: tuple[CorrectnessScenario, ...] = CORE_CORRECTNESS_SCENARIOS

    @model_validator(mode="after")
    def require_every_core_scenario_once(self) -> "RdsCorrectnessDefinition":
        if self.scenarios != CORE_CORRECTNESS_SCENARIOS:
            raise ValueError("RDS correctness must run every core scenario once")
        return self

    def test_run_id(self, scenario: CorrectnessScenario) -> UUID:
        """Derive a stable, collision-free run identity for one scenario."""
        return uuid5(self.suite_id, scenario.value)


class RdsScenarioEvidence(BaseModel):
    """Compact HTTP and reconciliation result for one scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    scenario: CorrectnessScenario
    test_run_id: UUID
    http_status_codes: tuple[int, ...]
    reconciliation: ReconciliationReport
    passed: Literal[True] = True

    @model_validator(mode="after")
    def require_passing_scenario_evidence(self) -> "RdsScenarioEvidence":
        if self.reconciliation.test_run_id != self.test_run_id:
            raise ValueError("reconciliation has the wrong test run ID")
        generated = self.reconciliation.generated
        expected_status_codes = (201,) * generated
        if self.scenario is CorrectnessScenario.DUPLICATE:
            unique = self.reconciliation.unique
            expected_status_codes = (201,) * unique + (200,) * (
                generated - unique
            )
        elif self.scenario is CorrectnessScenario.DOWNSTREAM_OUTAGE:
            expected_status_codes = (502,) * generated
        if self.http_status_codes != expected_status_codes:
            raise ValueError("HTTP outcomes do not prove the named scenario")
        if not self.reconciliation.invariants_passed:
            raise ValueError("scenario reconciliation did not pass")
        return self


class RdsCorrectnessEvidence(BaseModel):
    """Compact result returned from the private deployment through SSM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    definition: RdsCorrectnessDefinition
    scenario_results: tuple[RdsScenarioEvidence, ...]
    all_scenarios_passed: Literal[True] = True

    @model_validator(mode="after")
    def require_results_match_definition(self) -> "RdsCorrectnessEvidence":
        result_scenarios = tuple(
            result.scenario for result in self.scenario_results
        )
        if result_scenarios != self.definition.scenarios:
            raise ValueError("scenario results must match the suite definition")
        for result in self.scenario_results:
            if result.test_run_id != self.definition.test_run_id(
                result.scenario
            ):
                raise ValueError("scenario result has the wrong test run ID")
            if not result.reconciliation.invariants_passed:
                raise ValueError("scenario result failed reconciliation")
        return self


def execute_rds_correctness_suite(
    definition: RdsCorrectnessDefinition,
    *,
    output_root: Path,
    trackrelay_client: httpx.Client,
    downstream_client: httpx.Client,
    downstream_url: str,
) -> RdsCorrectnessEvidence:
    """Run all core scenarios through the shared correctness implementation."""
    configuration = GeneratorConfiguration(
        partner_id=definition.partner_id,
        shipment_count=definition.shipment_count,
        start_at=definition.start_at,
    )
    scenario_results = []
    for scenario in definition.scenarios:
        artifacts = execute_correctness_scenario(
            scenario,
            seed=definition.random_seed,
            configuration=configuration,
            output_root=output_root,
            trackrelay_client=trackrelay_client,
            downstream_client=downstream_client,
            downstream_url=downstream_url,
            test_run_id=definition.test_run_id(scenario),
        )
        observations = ScenarioObservations.model_validate_json(
            artifacts.observations_path.read_text(encoding="utf-8")
        )
        scenario_results.append(
            RdsScenarioEvidence(
                scenario=scenario,
                test_run_id=observations.test_run_id,
                http_status_codes=tuple(
                    observation.http_status_code
                    for observation in observations.requests
                ),
                reconciliation=artifacts.reconciliation,
            )
        )
    return RdsCorrectnessEvidence(
        definition=definition,
        scenario_results=tuple(scenario_results),
    )


def encoded_rds_correctness_evidence(
    evidence: RdsCorrectnessEvidence,
) -> str:
    """Encode compact evidence as one unambiguous SSM output line."""
    encoded = b64encode(evidence.model_dump_json().encode("utf-8")).decode(
        "ascii"
    )
    return f"{RDS_CORRECTNESS_EVIDENCE_PREFIX}{encoded}"


def build_parser() -> ArgumentParser:
    """Build the deliberately narrow private-helper command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--suite-id", type=UUID, required=True)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--shipments", type=int, default=1)
    parser.add_argument("--partner-id", default="rds-correctness-alpha")
    parser.add_argument("--start-at", default=DEFAULT_START_AT.isoformat())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run inside the deployed Compose network and emit compact evidence."""
    arguments = build_parser().parse_args(argv)
    definition = RdsCorrectnessDefinition(
        suite_id=arguments.suite_id,
        random_seed=arguments.seed,
        shipment_count=arguments.shipments,
        partner_id=arguments.partner_id,
        start_at=arguments.start_at,
    )
    settings = Settings()
    with (
        TemporaryDirectory(prefix="trackrelay-rds-correctness-") as output,
        httpx.Client(base_url="http://api:8000", timeout=10.0) as api_client,
        httpx.Client(
            base_url=settings.downstream_url,
            timeout=settings.downstream_timeout_seconds,
        ) as downstream_client,
    ):
        evidence = execute_rds_correctness_suite(
            definition,
            output_root=Path(output),
            trackrelay_client=api_client,
            downstream_client=downstream_client,
            downstream_url=settings.downstream_url,
        )
    print(encoded_rds_correctness_evidence(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
