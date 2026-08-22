"""Tests for the safe AWS CLI preflight."""

from collections.abc import Sequence
from json import dumps

from pytest import raises

from trackrelay.aws_preflight import AwsPreflightError, check_aws_access

PROFILE = "trackrelay-admin"
REGION = "ap-southeast-3"


class FakeAwsCli:
    """Return controlled AWS CLI responses and retain invoked arguments."""

    def __init__(self, *, region: str = REGION, arn: str) -> None:
        self.region = region
        self.arn = arn
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: Sequence[str]) -> str:
        call = tuple(arguments)
        self.calls.append(call)
        if call[:3] == ("configure", "get", "region"):
            return f"{self.region}\n"
        return dumps(
            {
                "UserId": "not-retained",
                "Account": "123456789012",
                "Arn": self.arn,
            }
        )


def test_check_uses_the_explicit_profile_and_region() -> None:
    cli = FakeAwsCli(arn="arn:aws:iam::123456789012:user/trackrelay")

    result = check_aws_access(
        profile=PROFILE,
        expected_region=REGION,
        command_runner=cli,
    )

    assert result.profile == PROFILE
    assert result.region == REGION
    assert result.principal_type == "IAM user"
    assert cli.calls == [
        ("configure", "get", "region", "--profile", PROFILE),
        (
            "--profile",
            PROFILE,
            "--region",
            REGION,
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ),
    ]


def test_check_accepts_an_assumed_role() -> None:
    cli = FakeAwsCli(
        arn=(
            "arn:aws:sts::123456789012:assumed-role/"
            "TrackRelayDeveloper/session"
        )
    )

    result = check_aws_access(
        profile=PROFILE,
        expected_region=REGION,
        command_runner=cli,
    )

    assert result.principal_type == "assumed role"


def test_check_rejects_the_root_user() -> None:
    cli = FakeAwsCli(arn="arn:aws:iam::123456789012:root")

    with raises(AwsPreflightError, match="root user"):
        check_aws_access(
            profile=PROFILE,
            expected_region=REGION,
            command_runner=cli,
        )


def test_check_rejects_a_different_configured_region() -> None:
    cli = FakeAwsCli(
        region="ap-southeast-1",
        arn="arn:aws:iam::123456789012:user/trackrelay",
    )

    with raises(AwsPreflightError, match="ap-southeast-1"):
        check_aws_access(
            profile=PROFILE,
            expected_region=REGION,
            command_runner=cli,
        )

    assert len(cli.calls) == 1


def test_check_rejects_an_invalid_identity_response() -> None:
    def invalid_cli(arguments: Sequence[str]) -> str:
        if tuple(arguments[:3]) == ("configure", "get", "region"):
            return REGION
        return "not-json"

    with raises(AwsPreflightError, match="invalid caller identity"):
        check_aws_access(
            profile=PROFILE,
            expected_region=REGION,
            command_runner=invalid_cli,
        )
