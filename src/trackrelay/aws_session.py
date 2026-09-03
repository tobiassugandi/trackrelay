"""Guard and record TrackRelay's bounded AWS cloud-session lifecycle."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from ipaddress import IPv4Network
from json import JSONDecodeError, dumps, loads
from pathlib import Path
from re import compile as compile_pattern
from subprocess import CompletedProcess, run
from typing import Literal

from trackrelay.aws_teardown import (
    AwsTeardownCheckError,
    cleanup_container_insights,
    inventory_rehost_resources,
)
from trackrelay.experiments.vertical_scaling import (
    ALLOWED_INSTANCE_TYPES,
    ECONOMICAL_BASELINE_INSTANCE_TYPE,
)
from trackrelay.operator_status import operator_status, status_activity

SESSION_ID_PATTERN = compile_pattern(r"^cloud-session-[1234]-[0-9]{8}T[0-9]{6}Z$")
CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]
NativeInventory = Callable[..., dict[str, int]]
DeploymentMode = Literal["rehost", "async"]


class AwsSessionError(RuntimeError):
    """A safe, actionable lifecycle failure."""


@dataclass(frozen=True)
class AwsSession:
    """Non-secret inputs shared by every lifecycle command."""

    session_id: str
    profile: str
    region: str
    api_ingress_cidr: str
    terraform_dir: Path
    evidence_root: Path
    rehost_instance_type: str = ECONOMICAL_BASELINE_INSTANCE_TYPE
    deployment_mode: DeploymentMode = "rehost"

    def __post_init__(self) -> None:
        if SESSION_ID_PATTERN.fullmatch(self.session_id) is None:
            raise AwsSessionError(
                "session ID must look like cloud-session-1-20260822T090000Z"
            )
        if not self.profile.strip():
            raise AwsSessionError("AWS profile must not be empty")
        if not self.region.strip():
            raise AwsSessionError("AWS region must not be empty")
        if self.rehost_instance_type not in ALLOWED_INSTANCE_TYPES:
            raise AwsSessionError(
                "rehost instance type must be one of the frozen "
                f"hardware tiers: {ALLOWED_INSTANCE_TYPES}"
            )
        if self.deployment_mode not in ("rehost", "async"):
            raise AwsSessionError("deployment mode must be either rehost or async")
        try:
            ingress_network = IPv4Network(self.api_ingress_cidr, strict=True)
        except ValueError as error:
            raise AwsSessionError(
                "API ingress must be one explicit IPv4 /32 CIDR"
            ) from error
        if ingress_network.prefixlen != 32:
            raise AwsSessionError("API ingress must be one explicit IPv4 /32 CIDR")

    @property
    def evidence_dir(self) -> Path:
        return self.evidence_root / self.session_id

    @property
    def plan_path(self) -> Path:
        return self.evidence_dir / "terraform.tfplan"

    @property
    def manifest_path(self) -> Path:
        return self.evidence_dir / "session.json"

    def terraform_command(self, *arguments: str) -> tuple[str, ...]:
        return (
            "terraform",
            f"-chdir={self.terraform_dir.resolve()}",
            *arguments,
        )

    def terraform_variables(self) -> tuple[str, ...]:
        return (
            f"-var=aws_profile={self.profile}",
            f"-var=aws_region={self.region}",
            f"-var=api_ingress_cidr={self.api_ingress_cidr}",
            f"-var=deployment_mode={self.deployment_mode}",
            f"-var=rehost_instance_type={self.rehost_instance_type}",
            f"-var=session_id={self.session_id}",
        )


def run_command(arguments: Sequence[str]) -> CompletedProcess[str]:
    """Run a command without streaming potentially identifying output."""
    return run(
        arguments,
        capture_output=True,
        check=False,
        text=True,
    )


def write_command_log(path: Path, result: CompletedProcess[str]) -> None:
    """Save ignored local evidence without printing it to chat or stdout."""
    path.write_text(
        f"exit_code: {result.returncode}\n\n"
        f"stdout:\n{result.stdout}\n\n"
        f"stderr:\n{result.stderr}\n",
        encoding="utf-8",
    )


def require_success(
    *,
    result: CompletedProcess[str],
    action: str,
    log_path: Path,
) -> None:
    """Fail safely and point to the private local diagnostic log."""
    write_command_log(log_path, result)
    if result.returncode != 0:
        raise AwsSessionError(f"{action} failed; inspect {log_path}")


def file_sha256(path: Path) -> str:
    """Hash a saved Terraform plan so approval binds to exact bytes."""
    return sha256(path.read_bytes()).hexdigest()


def read_git_revision(runner: CommandRunner = run_command) -> str:
    """Return the exact repository revision used to create a plan."""
    result = runner(("git", "rev-parse", "HEAD"))
    if result.returncode != 0 or not result.stdout.strip():
        raise AwsSessionError("could not identify the current Git revision")
    return result.stdout.strip()


def read_clean_git_revision(runner: CommandRunner = run_command) -> str:
    """Require a clean worktree and return its committed revision."""
    status = runner(("git", "status", "--porcelain"))
    if status.returncode != 0:
        raise AwsSessionError("could not inspect the Git worktree")
    if status.stdout.strip():
        raise AwsSessionError("Git worktree must be clean")
    return read_git_revision(runner)


def write_manifest(session: AwsSession, manifest: dict[str, object]) -> None:
    """Write compact, non-secret lifecycle metadata."""
    session.manifest_path.write_text(
        dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(session: AwsSession) -> dict[str, object]:
    """Load and validate the session metadata required by later commands."""
    try:
        manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, JSONDecodeError) as error:
        raise AwsSessionError(
            f"valid session metadata not found at {session.manifest_path}"
        ) from error
    if not isinstance(manifest, dict):
        raise AwsSessionError("session metadata must be a JSON object")
    for field, expected in (
        ("session_id", session.session_id),
        ("profile", session.profile),
        ("region", session.region),
        ("api_ingress_cidr", session.api_ingress_cidr),
        ("rehost_instance_type", session.rehost_instance_type),
    ):
        if manifest.get(field) != expected:
            raise AwsSessionError(
                f"session metadata {field} does not match this command"
            )
    if manifest.get("deployment_mode", "rehost") != session.deployment_mode:
        raise AwsSessionError(
            "session metadata deployment_mode does not match this command"
        )
    return manifest


def plan_session(
    session: AwsSession,
    *,
    runner: CommandRunner = run_command,
    git_revision: str | None = None,
) -> None:
    """Save a speculative Terraform plan and its approval identity."""
    if session.manifest_path.exists() or session.plan_path.exists():
        raise AwsSessionError(
            "session ID already has planning evidence; choose a new ID"
        )
    resolved_revision = git_revision or read_clean_git_revision(runner)
    session.evidence_dir.mkdir(parents=True, exist_ok=True)
    result = runner(
        session.terraform_command(
            "plan",
            "-input=false",
            f"-out={session.plan_path.resolve()}",
            *session.terraform_variables(),
        )
    )
    require_success(
        result=result,
        action="Terraform plan",
        log_path=session.evidence_dir / "terraform-plan.log",
    )
    if not session.plan_path.is_file():
        raise AwsSessionError("Terraform did not create the expected plan file")

    write_manifest(
        session,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "deployment_mode": session.deployment_mode,
            "git_revision": resolved_revision,
            "api_ingress_cidr": session.api_ingress_cidr,
            "plan_sha256": file_sha256(session.plan_path),
            "profile": session.profile,
            "rehost_instance_type": session.rehost_instance_type,
            "region": session.region,
            "schema_version": 1,
            "session_id": session.session_id,
            "status": "planned",
        },
    )


def parse_positive_money(value: str, *, field_name: str) -> Decimal:
    """Parse an explicit positive USD guardrail without float rounding."""
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise AwsSessionError(f"{field_name} must be a USD amount") from error
    if not amount.is_finite() or amount <= 0:
        raise AwsSessionError(f"{field_name} must be greater than zero")
    return amount


def apply_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    monthly_budget_usd: str,
    runner: CommandRunner = run_command,
    git_revision: str | None = None,
) -> None:
    """Apply only the exact saved plan after mechanical approval checks."""
    if approved_session_id != session.session_id:
        raise AwsSessionError("APPROVED_SESSION_ID must exactly match SESSION_ID")
    ceiling = parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    )
    monthly_budget = parse_positive_money(
        monthly_budget_usd,
        field_name="monthly budget",
    )
    if ceiling > monthly_budget:
        raise AwsSessionError("approved cost ceiling exceeds the monthly AWS budget")

    manifest = load_manifest(session)
    if not session.plan_path.is_file():
        raise AwsSessionError("saved Terraform plan is missing")
    if manifest.get("plan_sha256") != file_sha256(session.plan_path):
        raise AwsSessionError("saved Terraform plan changed after planning")
    current_revision = git_revision or read_clean_git_revision(runner)
    if manifest.get("git_revision") != current_revision:
        raise AwsSessionError("Git revision changed after planning")

    result = runner(
        session.terraform_command(
            "apply",
            "-input=false",
            session.plan_path.resolve().as_posix(),
        )
    )
    require_success(
        result=result,
        action="Terraform apply",
        log_path=session.evidence_dir / "terraform-apply.log",
    )
    manifest.update(
        {
            "applied_at": datetime.now(UTC).isoformat(),
            "approved_cost_ceiling_usd": str(ceiling),
            "status": "applied",
        }
    )
    write_manifest(session, manifest)


def destroy_session(
    session: AwsSession,
    *,
    runner: CommandRunner = run_command,
    telemetry_cleaner: Callable[..., None] = cleanup_container_insights,
) -> None:
    """Destroy without an approval gate and retain both command logs."""
    manifest = load_manifest(session) if session.manifest_path.exists() else None
    session.evidence_dir.mkdir(parents=True, exist_ok=True)
    destroy_plan = session.evidence_dir / "terraform-destroy.tfplan"
    with status_activity("Cleanup: preparing Terraform destroy plan"):
        plan_result = runner(
            session.terraform_command(
                "plan",
                "-destroy",
                "-input=false",
                f"-out={destroy_plan.resolve()}",
                *session.terraform_variables(),
            )
        )
        require_success(
            result=plan_result,
            action="Terraform destroy plan",
            log_path=session.evidence_dir / "terraform-destroy-plan.log",
        )
    with status_activity("Cleanup: applying Terraform destroy plan"):
        apply_result = runner(
            session.terraform_command(
                "apply",
                "-input=false",
                destroy_plan.resolve().as_posix(),
            )
        )
        require_success(
            result=apply_result,
            action="Terraform destroy",
            log_path=session.evidence_dir / "terraform-destroy.log",
        )

    if manifest is not None:
        manifest.update(
            {
                "destroyed_at": datetime.now(UTC).isoformat(),
                "status": "destroyed_pending_verification",
            }
        )
        write_manifest(session, manifest)

    if session.deployment_mode == "async":
        state_result = runner(session.terraform_command("state", "list"))
        state_log = (
            session.evidence_dir / "terraform-state-before-telemetry-cleanup.log"
        )
        if (
            state_result.returncode
            and "No state file was found!" in state_result.stderr
        ):
            write_command_log(state_log, state_result)
        else:
            require_success(
                result=state_result,
                action="Terraform state check before telemetry cleanup",
                log_path=state_log,
            )
        if state_result.stdout.strip():
            raise AwsSessionError("refusing telemetry cleanup with managed resources")
        try:
            with status_activity(
                "Cleanup: waiting for late Container Insights absence"
            ):
                telemetry_cleaner(
                    profile=session.profile,
                    region=session.region,
                    session_id=session.session_id,
                    runner=runner,
                    evidence_path=session.evidence_dir
                    / "container-insights-cleanup.json",
                )
        except AwsTeardownCheckError as error:
            raise AwsSessionError(str(error)) from error


def verify_destroyed(
    session: AwsSession,
    *,
    runner: CommandRunner = run_command,
    native_inventory: NativeInventory = inventory_rehost_resources,
) -> None:
    """Verify empty state plus generic and native AWS inventories."""
    manifest = load_manifest(session) if session.manifest_path.exists() else None
    session.evidence_dir.mkdir(parents=True, exist_ok=True)
    operator_status("Teardown verification: checking Terraform state")
    state_result = runner(session.terraform_command("state", "list"))
    state_log_path = session.evidence_dir / "terraform-state-after-destroy.log"
    no_state_exists = (
        state_result.returncode != 0
        and "No state file was found!" in state_result.stderr
    )
    if no_state_exists:
        write_command_log(state_log_path, state_result)
    else:
        require_success(
            result=state_result,
            action="Terraform state verification",
            log_path=state_log_path,
        )
    if state_result.stdout.strip():
        raise AwsSessionError("Terraform state still contains managed resources")

    operator_status("Teardown verification: checking tagged-resource inventory")
    inventory_result = runner(
        (
            "aws",
            "--profile",
            session.profile,
            "--region",
            session.region,
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            "Key=Project,Values=TrackRelay",
            f"Key=SessionId,Values={session.session_id}",
            "--output",
            "json",
        )
    )
    if inventory_result.returncode != 0:
        raise AwsSessionError("AWS tagged-resource inventory failed")
    try:
        inventory = loads(inventory_result.stdout)
        resources = inventory["ResourceTagMappingList"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsSessionError("AWS returned an invalid resource inventory") from error
    if not isinstance(resources, list):
        raise AwsSessionError("AWS returned an invalid resource inventory")

    sanitized_inventory = {
        "get_resources_semantics": "tagged-or-previously-tagged",
        "returned_record_count": len(resources),
        "session_id": session.session_id,
    }
    (session.evidence_dir / "aws-inventory-after-destroy.json").write_text(
        dumps(sanitized_inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with status_activity(
            "Teardown verification: checking service-native inventory"
        ):
            native_counts = native_inventory(
                profile=session.profile,
                region=session.region,
                session_id=session.session_id,
                runner=runner,
            )
    except AwsTeardownCheckError as error:
        raise AwsSessionError(str(error)) from error
    (session.evidence_dir / "aws-native-inventory-after-destroy.json").write_text(
        dumps(native_counts, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    remaining_native_resources = {
        name: count for name, count in native_counts.items() if count
    }
    if remaining_native_resources:
        raise AwsSessionError(
            "AWS native checks still report session-owned resources: "
            f"{sorted(remaining_native_resources)}"
        )

    if manifest is not None:
        manifest.update(
            {
                "tag_index_record_count_after_destroy": len(resources),
                "teardown_verified_at": datetime.now(UTC).isoformat(),
                "status": "teardown_verified",
            }
        )
        write_manifest(session, manifest)
    operator_status(
        f"Native absence confirmed: {len(native_counts)}/{len(native_counts)} categories zero"
    )


def add_shared_arguments(parser: ArgumentParser) -> None:
    """Add non-secret arguments shared by every lifecycle command."""
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--api-ingress-cidr", required=True)
    parser.add_argument(
        "--deployment-mode",
        choices=("rehost", "async"),
        default="rehost",
    )
    parser.add_argument(
        "--rehost-instance-type",
        choices=ALLOWED_INSTANCE_TYPES,
        default=ECONOMICAL_BASELINE_INSTANCE_TYPE,
    )
    parser.add_argument("--terraform-dir", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)


def build_parser() -> ArgumentParser:
    """Build the cloud-session lifecycle command parser."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "destroy", "verify"):
        add_shared_arguments(subparsers.add_parser(command))
    apply_parser = subparsers.add_parser("apply")
    add_shared_arguments(apply_parser)
    apply_parser.add_argument("--approved-session-id", required=True)
    apply_parser.add_argument("--approved-cost-ceiling-usd", required=True)
    apply_parser.add_argument("--monthly-budget-usd", required=True)
    return parser


def session_from_arguments(arguments: Namespace) -> AwsSession:
    """Construct validated lifecycle inputs from CLI arguments."""
    return AwsSession(
        session_id=arguments.session_id,
        profile=arguments.profile,
        region=arguments.region,
        api_ingress_cidr=arguments.api_ingress_cidr,
        terraform_dir=arguments.terraform_dir,
        evidence_root=arguments.evidence_root,
        rehost_instance_type=arguments.rehost_instance_type,
        deployment_mode=arguments.deployment_mode,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one cloud-session lifecycle command."""
    arguments = build_parser().parse_args(argv)
    try:
        session = session_from_arguments(arguments)
        if arguments.command == "plan":
            plan_session(session)
            print(f"saved Terraform plan for {session.session_id}")
        elif arguments.command == "apply":
            apply_session(
                session,
                approved_session_id=arguments.approved_session_id,
                approved_cost_ceiling_usd=(arguments.approved_cost_ceiling_usd),
                monthly_budget_usd=arguments.monthly_budget_usd,
            )
            print(f"applied approved Terraform plan for {session.session_id}")
        elif arguments.command == "destroy":
            destroy_session(session)
            print(f"destroyed Terraform resources for {session.session_id}")
        else:
            verify_destroyed(session)
            print("Terraform, tagged, and native teardown checks passed")
    except AwsSessionError as error:
        raise SystemExit(f"AWS session command failed: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
