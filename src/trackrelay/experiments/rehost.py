"""Prepare and reconcile a rehost workload inside its private network."""

from argparse import ArgumentParser
from base64 import b64encode
from collections.abc import Sequence
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.config import Settings
from trackrelay.database import session_factory as default_session_factory
from trackrelay.downstream.control import SimulatorMode
from trackrelay.experiments.generator import DEFAULT_START_AT, InputManifest
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    build_performance_manifest,
    prepare_load_partner,
    wait_for_database_run_to_settle,
)
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    fetch_simulator_receipts,
    reconcile_manifest,
)
from trackrelay.experiments.scenarios import (
    complete_database_run,
    prepare_database_for_run,
)

PositiveInteger = Annotated[int, Field(gt=0)]
EVIDENCE_PREFIX = "TRACKRELAY_REHOST_EVIDENCE="


class RehostWorkloadPoint(BaseModel):
    """Identity and frozen inputs for one healthy rehost load point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    request_rate_per_second: PositiveInteger
    duration_seconds: PositiveInteger
    random_seed: int = 20260806
    partner_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    start_at: AwareDatetime = DEFAULT_START_AT
    post_load_settle_timeout_seconds: Annotated[float, Field(gt=0)] = 30
    post_load_stable_window_seconds: Annotated[float, Field(gt=0)] = 2

    def performance_configuration(self) -> PerformanceExperimentConfiguration:
        """Translate the portable point into the shared workload contract."""
        return PerformanceExperimentConfiguration(
            scenario=LoadScenario.HEALTHY,
            request_rate_per_second=self.request_rate_per_second,
            duration_seconds=self.duration_seconds,
            random_seed=self.random_seed,
            partner_id=self.partner_id,
            start_at=self.start_at,
            post_load_settle_timeout_seconds=(
                self.post_load_settle_timeout_seconds
            ),
            post_load_stable_window_seconds=(
                self.post_load_stable_window_seconds
            ),
        )

    def manifest(self) -> InputManifest:
        """Regenerate exactly the manifest used by the external driver."""
        return build_performance_manifest(
            self.performance_configuration(),
            test_run_id=self.test_run_id,
        )


class RehostServerEvidence(BaseModel):
    """Compact private-side evidence safe to return through SSM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    point: RehostWorkloadPoint
    reconciliation: ReconciliationReport


def prepare_rehost_workload_point(
    point: RehostWorkloadPoint,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
    downstream_client: httpx.Client,
) -> None:
    """Create private database state and require a healthy simulator."""
    manifest = point.manifest()
    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": SimulatorMode.HEALTHY.value},
    )
    mode_response.raise_for_status()
    prepare_load_partner(point.partner_id, sessions=sessions)
    prepare_database_for_run(manifest, sessions=sessions)


def collect_rehost_workload_evidence(
    point: RehostWorkloadPoint,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
    downstream_client: httpx.Client,
) -> RehostServerEvidence:
    """Settle and reconcile evidence without exposing private services."""
    manifest = point.manifest()
    configuration = point.performance_configuration()
    wait_for_database_run_to_settle(
        point.test_run_id,
        expected_request_count=configuration.expected_request_count,
        timeout_seconds=configuration.post_load_settle_timeout_seconds,
        stable_window_seconds=(
            configuration.post_load_stable_window_seconds
        ),
        sessions=sessions,
    )
    complete_database_run(point.test_run_id, sessions=sessions)
    simulator_receipts = fetch_simulator_receipts(
        str(downstream_client.base_url),
        client=downstream_client,
        test_run_id=point.test_run_id,
    )
    with sessions() as session:
        reconciliation = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )
    return RehostServerEvidence(
        point=point,
        reconciliation=reconciliation,
    )


def encoded_evidence(evidence: RehostServerEvidence) -> str:
    """Encode one compact JSON object as an unambiguous output line."""
    encoded = b64encode(evidence.model_dump_json().encode("utf-8")).decode(
        "ascii"
    )
    return f"{EVIDENCE_PREFIX}{encoded}"


def build_parser() -> ArgumentParser:
    """Build the private helper's deliberately narrow command line."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "collect"))
    parser.add_argument("--test-run-id", type=UUID, required=True)
    parser.add_argument("--rate", type=int, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--partner-id", required=True)
    parser.add_argument("--start-at", required=True)
    parser.add_argument(
        "--settle-timeout-seconds",
        type=float,
        required=True,
    )
    parser.add_argument(
        "--stable-window-seconds",
        type=float,
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run within the deployed Compose network, normally through SSM."""
    arguments = build_parser().parse_args(argv)
    point = RehostWorkloadPoint(
        test_run_id=arguments.test_run_id,
        request_rate_per_second=arguments.rate,
        duration_seconds=arguments.duration_seconds,
        random_seed=arguments.seed,
        partner_id=arguments.partner_id,
        start_at=arguments.start_at,
        post_load_settle_timeout_seconds=arguments.settle_timeout_seconds,
        post_load_stable_window_seconds=arguments.stable_window_seconds,
    )
    settings = Settings()
    with httpx.Client(
        base_url=settings.downstream_url,
        timeout=settings.downstream_timeout_seconds,
    ) as downstream_client:
        if arguments.action == "prepare":
            prepare_rehost_workload_point(
                point,
                downstream_client=downstream_client,
            )
            print("TRACKRELAY_REHOST_PREPARED")
        else:
            evidence = collect_rehost_workload_evidence(
                point,
                downstream_client=downstream_client,
            )
            print(encoded_evidence(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
