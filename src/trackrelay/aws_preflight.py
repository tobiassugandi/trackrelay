"""Verify TrackRelay's non-secret AWS CLI configuration without changing AWS."""

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from json import JSONDecodeError, loads
from shutil import which
from subprocess import run

CommandRunner = Callable[[Sequence[str]], str]


class AwsPreflightError(RuntimeError):
    """A safe, actionable AWS preflight failure."""


@dataclass(frozen=True)
class AwsPreflightResult:
    """Non-secret facts proven by the AWS preflight."""

    profile: str
    region: str
    principal_type: str


def run_aws_command(arguments: Sequence[str]) -> str:
    """Run one read-only AWS CLI command without echoing its response."""
    if which("aws") is None:
        raise AwsPreflightError("AWS CLI is not installed or is not on PATH")

    completed = run(
        ("aws", *arguments),
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AwsPreflightError(
            "AWS CLI authentication or configuration check failed"
        )
    return completed.stdout


def classify_principal(arn: str) -> str:
    """Describe an AWS ARN without retaining or displaying its identity."""
    if arn.endswith(":root"):
        return "root"
    if ":assumed-role/" in arn:
        return "assumed role"
    if ":user/" in arn:
        return "IAM user"
    return "non-root principal"


def check_aws_access(
    *,
    profile: str,
    expected_region: str,
    command_runner: CommandRunner = run_aws_command,
) -> AwsPreflightResult:
    """Verify the selected profile, region, and non-root AWS identity."""
    configured_region = command_runner(
        ("configure", "get", "region", "--profile", profile)
    ).strip()
    if configured_region != expected_region:
        raise AwsPreflightError(
            f"profile {profile!r} uses region {configured_region!r}; "
            f"expected {expected_region!r}"
        )

    raw_identity = command_runner(
        (
            "--profile",
            profile,
            "--region",
            expected_region,
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        )
    )
    try:
        identity = loads(raw_identity)
        arn = identity["Arn"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsPreflightError(
            "AWS CLI returned an invalid caller identity response"
        ) from error
    if not isinstance(arn, str):
        raise AwsPreflightError(
            "AWS CLI returned an invalid caller identity response"
        )

    principal_type = classify_principal(arn)
    if principal_type == "root":
        raise AwsPreflightError(
            "the selected AWS CLI profile authenticates as the root user"
        )

    return AwsPreflightResult(
        profile=profile,
        region=expected_region,
        principal_type=principal_type,
    )


def build_parser() -> ArgumentParser:
    """Build the command-line parser."""
    parser = ArgumentParser(
        description=(
            "Verify TrackRelay's AWS profile, region, and non-root identity "
            "without changing AWS resources."
        )
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the safe AWS configuration preflight."""
    arguments = build_parser().parse_args(argv)
    try:
        result = check_aws_access(
            profile=arguments.profile,
            expected_region=arguments.region,
        )
    except AwsPreflightError as error:
        raise SystemExit(f"AWS preflight failed: {error}") from error

    print("AWS preflight passed")
    print(f"profile: {result.profile}")
    print(f"region: {result.region}")
    print(f"principal type: {result.principal_type}")
    print("resource changes: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
