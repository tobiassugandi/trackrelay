"""Collect compact correctness evidence from the private RDS rehost."""

from base64 import b64decode
from collections.abc import Callable
from datetime import UTC, datetime
from json import dumps
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from trackrelay.aws_rehost import (
    INSTANCE_ID_PATTERN,
    AwsRehostError,
    ProcessRunner,
    aws_prefix,
    invoke,
    require_applied_clean_revision,
    run_process,
    run_ssm_payload,
    terraform_output,
)
from trackrelay.aws_session import AwsSession, write_manifest
from trackrelay.experiments.rds_correctness import (
    RDS_CORRECTNESS_EVIDENCE_PREFIX,
    RdsCorrectnessDefinition,
    RdsCorrectnessEvidence,
)


class RdsCorrectnessSummary(BaseModel):
    """Small session index pointing to the complete compact evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    completed_at: AwareDatetime
    scenario_count: Literal[4] = 4
    all_scenarios_passed: Literal[True] = True


def build_rds_correctness_payload(
    definition: RdsCorrectnessDefinition,
) -> dict[str, list[str]]:
    """Build a secret-free command that stays inside the Compose network."""
    command = " ".join(
        (
            "docker compose",
            "--project-name trackrelay-rehost",
            "--env-file /opt/trackrelay/.env",
            "--file /opt/trackrelay/compose.yaml",
            "run --rm --no-deps api",
            "python -m trackrelay.experiments.rds_correctness",
            f"--suite-id {definition.suite_id}",
            f"--seed {definition.random_seed}",
            f"--shipments {definition.shipment_count}",
            f"--partner-id {definition.partner_id}",
            f"--start-at {definition.start_at.isoformat()}",
        )
    )
    payload = {
        "commands": [f"set -euo pipefail\n{command}"],
        "executionTimeout": ["300"],
    }
    if len(dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM correctness payload exceeds the safety limit")
    return payload


def read_rds_correctness_evidence(
    session: AwsSession,
    *,
    instance_id: str,
    command_id: str,
    definition: RdsCorrectnessDefinition,
    runner: ProcessRunner,
) -> RdsCorrectnessEvidence:
    """Read and validate exactly one compact result from SSM stdout."""
    output = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "StandardOutputContent",
            "--output",
            "text",
        ),
        action="SSM RDS correctness evidence collection",
    ).stdout
    evidence_lines = [
        line.removeprefix(RDS_CORRECTNESS_EVIDENCE_PREFIX)
        for line in output.splitlines()
        if line.startswith(RDS_CORRECTNESS_EVIDENCE_PREFIX)
    ]
    if len(evidence_lines) != 1:
        raise AwsRehostError("SSM returned ambiguous RDS correctness evidence")
    try:
        evidence_json = b64decode(
            evidence_lines[0],
            validate=True,
        ).decode("utf-8")
        evidence = RdsCorrectnessEvidence.model_validate_json(evidence_json)
    except (ValueError, UnicodeDecodeError) as error:
        raise AwsRehostError("SSM returned invalid RDS correctness evidence") from error
    if evidence.definition != definition:
        raise AwsRehostError("RDS correctness evidence identity differs")
    return evidence


def collect_rds_correctness(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RdsCorrectnessEvidence:
    """Run all four scenarios against RDS and save non-secret evidence."""
    manifest, _ = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "rds_deployed":
        raise AwsRehostError("the RDS-backed rehost has not been deployed")
    instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")

    definition = RdsCorrectnessDefinition(suite_id=uuid4())
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_rds_correctness_payload(definition),
        comment="TrackRelay RDS correctness suite",
        runner=runner,
    )
    evidence = read_rds_correctness_evidence(
        session,
        instance_id=instance_id,
        command_id=command_id,
        definition=definition,
        runner=runner,
    )
    completed_at = now()
    summary = RdsCorrectnessSummary(completed_at=completed_at)
    output_root = session.evidence_dir / "rds-correctness"
    output_root.mkdir(parents=True, exist_ok=False)
    evidence_path = output_root / "suite.json"
    evidence_path.write_text(
        f"{evidence.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    manifest.update(
        {
            "rds_correctness": {
                **summary.model_dump(mode="json"),
                "result": "rds-correctness/suite.json",
            },
            "status": "rds_correctness_collected",
        }
    )
    write_manifest(session, manifest)
    return evidence
