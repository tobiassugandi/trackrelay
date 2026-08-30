"""Build the short Stage 9.3 cloud hardware-flexibility result."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from trackrelay.aws_rehost import (
    AwsRehostError,
    ProcessRunner,
    require_applied_clean_revision,
    run_process,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    session_from_arguments,
    write_manifest,
)
from trackrelay.aws_vertical_scaling import (
    VerticalScalingRateAssessment,
    VerticalScalingTierSummary,
    VerticalScalingTransitionEvidence,
    _load_prepared_definition,
)
from trackrelay.experiments.vertical_scaling import (
    PRIMARY_FLEXIBILITY_INSTANCE_TYPES,
)


class ShortTierResult(BaseModel):
    """The observed pass/fail boundary for one machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_type: str
    tested_rates_per_second: tuple[int, ...]
    maximum_passing_rate_per_second: int | None
    first_failing_rate_per_second: int | None


class HardwareFlexibilityReport(BaseModel):
    """The one result needed for the short cloud-flexibility claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    experiment_name: Literal["aws-synchronous-hardware-flexibility-v4"] = (
        "aws-synchronous-hardware-flexibility-v4"
    )
    generated_at: AwareDatetime
    source: ShortTierResult
    target: ShortTierResult
    stronger_machine_passed_a_higher_rate: bool
    conclusion: str

    @model_validator(mode="after")
    def require_the_frozen_comparison(self) -> "HardwareFlexibilityReport":
        if (self.source.instance_type, self.target.instance_type) != (
            PRIMARY_FLEXIBILITY_INSTANCE_TYPES
        ):
            raise ValueError("report must compare the frozen two-machine order")
        source_rate = self.source.maximum_passing_rate_per_second or 0
        target_rate = self.target.maximum_passing_rate_per_second or 0
        if self.stronger_machine_passed_a_higher_rate is not (
            target_rate > source_rate
        ):
            raise ValueError("improvement flag differs from the observed rates")
        return self


def _read_model[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AwsRehostError(f"invalid Stage 9.3 evidence: {path}") from error


def _load_tier(
    session: AwsSession,
    instance_type: str,
    *,
    expected_rates: tuple[int, ...],
) -> ShortTierResult:
    root = session.evidence_dir / "vertical-scaling" / "tiers" / instance_type
    summary = _read_model(root / "summary.json", VerticalScalingTierSummary)
    if summary.instance_type != instance_type:
        raise AwsRehostError("tier summary has the wrong machine identity")
    observed_rates = tuple(
        assessment.offered_rate_per_second
        for assessment in summary.rate_assessments
    )
    if observed_rates != expected_rates[: len(observed_rates)]:
        raise AwsRehostError("tier summary differs from the frozen rate ladder")
    for assessment in summary.rate_assessments:
        saved = _read_model(
            root
            / "rates"
            / str(assessment.offered_rate_per_second)
            / "assessment.json",
            VerticalScalingRateAssessment,
        )
        if saved != assessment:
            raise AwsRehostError("saved rate assessment differs from tier summary")
    return ShortTierResult(
        instance_type=instance_type,
        tested_rates_per_second=tuple(
            assessment.offered_rate_per_second
            for assessment in summary.rate_assessments
        ),
        maximum_passing_rate_per_second=(
            summary.maximum_sustainable_rate_per_second
        ),
        first_failing_rate_per_second=summary.first_failing_rate_per_second,
    )


def _validate_transition(session: AwsSession, manifest: dict[str, object]) -> None:
    scaling = manifest.get("vertical_scaling")
    if not isinstance(scaling, dict):
        raise AwsRehostError("Stage 9.3 state is missing")
    transitions = scaling.get("transitions")
    if not isinstance(transitions, list) or len(transitions) != 1:
        raise AwsRehostError("the two-machine transition is incomplete")
    transition = transitions[0]
    if not isinstance(transition, dict):
        raise AwsRehostError("the transition journal is invalid")
    expected = PRIMARY_FLEXIBILITY_INSTANCE_TYPES
    if (
        transition.get("source_instance_type"),
        transition.get("target_instance_type"),
    ) != expected:
        raise AwsRehostError("the transition journal has the wrong tier order")
    evidence_path = transition.get("evidence")
    if not isinstance(evidence_path, str):
        raise AwsRehostError("transition evidence path is missing")
    relative_evidence_path = Path(evidence_path)
    evidence_root = session.evidence_dir.resolve()
    resolved_evidence_path = (evidence_root / relative_evidence_path).resolve()
    if relative_evidence_path.is_absolute() or not resolved_evidence_path.is_relative_to(
        evidence_root
    ):
        raise AwsRehostError("transition evidence path escapes the session")
    evidence = _read_model(
        resolved_evidence_path,
        VerticalScalingTransitionEvidence,
    )
    if (evidence.source_instance_type, evidence.target_instance_type) != expected:
        raise AwsRehostError("transition evidence has the wrong tier order")


def _render_markdown(report: HardwareFlexibilityReport) -> str:
    def rate(value: int | None) -> str:
        return "none" if value is None else f"{value}/s"

    return "\n".join(
        (
            "# Stage 9.3 short hardware-flexibility result",
            "",
            (
                "The same synchronous TrackRelay revision, container image, RDS "
                "database, downstream, and load driver were used for both machines."
            ),
            "",
            "| Machine | Tested rates | Highest passing | First failing |",
            "| --- | --- | ---: | ---: |",
            (
                f"| `{report.source.instance_type}` | "
                f"{', '.join(map(str, report.source.tested_rates_per_second))} | "
                f"{rate(report.source.maximum_passing_rate_per_second)} | "
                f"{rate(report.source.first_failing_rate_per_second)} |"
            ),
            (
                f"| `{report.target.instance_type}` | "
                f"{', '.join(map(str, report.target.tested_rates_per_second))} | "
                f"{rate(report.target.maximum_passing_rate_per_second)} | "
                f"{rate(report.target.first_failing_rate_per_second)} |"
            ),
            "",
            f"**Result:** {report.conclusion}",
            "",
            (
                "This is a short demonstration of cloud hardware flexibility, "
                "not a long-duration capacity estimate or an autoscaling result."
            ),
            "",
        )
    )


def generate_hardware_flexibility_report(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HardwareFlexibilityReport:
    """Validate two completed tiers and write the intentionally small result."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    scaling = manifest.get("vertical_scaling")
    expected_tiers = list(PRIMARY_FLEXIBILITY_INSTANCE_TYPES)
    if (
        manifest.get("status") != "vertical_scaling_tier_collected"
        or not isinstance(scaling, dict)
        or scaling.get("current_tier") != expected_tiers[-1]
        or scaling.get("completed_tiers") != expected_tiers
    ):
        raise AwsRehostError("both short Stage 9.3 machine runs must be complete")
    definition, _ = _load_prepared_definition(
        session,
        manifest=manifest,
        revision=revision,
        require_current_incomplete=False,
    )
    _validate_transition(session, manifest)
    source, target = (
        _load_tier(
            session,
            instance_type,
            expected_rates=definition.controls.workload.offered_rates_per_second,
        )
        for instance_type in PRIMARY_FLEXIBILITY_INSTANCE_TYPES
    )
    improved = (target.maximum_passing_rate_per_second or 0) > (
        source.maximum_passing_rate_per_second or 0
    )
    source_rate = (
        "no passing point"
        if source.maximum_passing_rate_per_second is None
        else f"{source.maximum_passing_rate_per_second}/s"
    )
    target_rate = (
        "no passing point"
        if target.maximum_passing_rate_per_second is None
        else f"{target.maximum_passing_rate_per_second}/s"
    )
    conclusion = (
        f"Changing only the EC2 instance type from {source.instance_type} to "
        f"{target.instance_type} increased the highest passing short-run rate "
        f"from {source_rate} to {target_rate}."
        if improved
        else "The short ladder did not yet show a higher passing rate on the "
        "stronger machine; run the same predeclared refinement rates on both "
        "machines before making a flexibility claim."
    )
    report = HardwareFlexibilityReport(
        generated_at=now(),
        source=source,
        target=target,
        stronger_machine_passed_a_higher_rate=improved,
        conclusion=conclusion,
    )
    report_root = session.evidence_dir / "vertical-scaling" / "report"
    report_root.mkdir(parents=True, exist_ok=False)
    json_path = report_root / "comparison-report.json"
    markdown_path = report_root / "comparison-report.md"
    json_path.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    scaling["report"] = {
        "json": str(json_path.relative_to(session.evidence_dir)),
        "markdown": str(markdown_path.relative_to(session.evidence_dir)),
    }
    manifest["status"] = "vertical_scaling_reported"
    write_manifest(session, manifest)
    return report


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    return parser


def generate_from_arguments(arguments: Namespace) -> HardwareFlexibilityReport:
    return generate_hardware_flexibility_report(session_from_arguments(arguments))


def main(argv: list[str] | None = None) -> int:
    try:
        report = generate_from_arguments(build_parser().parse_args(argv))
    except (AwsRehostError, AwsSessionError) as error:
        raise SystemExit(f"AWS hardware-flexibility report failed: {error}") from error
    print(report.conclusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
