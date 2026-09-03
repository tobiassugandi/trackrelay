"""Failure evidence must survive nested cleanup errors without exposing secrets."""

from hashlib import sha256
from json import dumps, loads
from subprocess import CompletedProcess

from tests.test_aws_elasticity_cloudwatch import session
from trackrelay.aws_diagnostics import capture_ecs_diagnostics, error_evidence
from trackrelay.aws_fixed_control import AwsFixedControlCleanupError
from trackrelay.aws_session import AwsSessionError


def test_nested_failures_expose_safe_descriptions_not_arbitrary_payloads():
    error = AwsFixedControlCleanupError(
        "fixed failed",
        workflow_error=RuntimeError("secret-password"),
        cleanup_errors=[AwsSessionError("native verification failed")],
    )
    evidence = error_evidence(error)
    assert evidence["cause"] == {"type": "RuntimeError"}
    assert evidence["cleanup"][0]["message"] == "native verification failed"
    assert "secret-password" not in dumps(evidence)


def test_collects_task_stop_reason_without_environment_or_account_arns(tmp_path):
    s = session(tmp_path)
    cluster = f"trackrelay-{sha256(s.session_id.encode()).hexdigest()[:8]}-async"
    arn = f"arn:aws:ecs:region:123456789012:task/{cluster}/abc"

    def runner(command):
        document = {"taskArns": [arn]}
        if "describe-tasks" in command:
            document = {
                "tasks": [
                    {
                        "taskArn": arn,
                        "stoppedReason": "Task failed container health checks",
                        "containers": [
                            {
                                "name": "simulator",
                                "exitCode": 143,
                                "environment": [
                                    {"name": "PASSWORD", "value": "secret"}
                                ],
                            }
                        ],
                    }
                ]
            }
        return CompletedProcess(command, 0, dumps(document), "")

    capture_ecs_diagnostics(s, runner=runner)
    capture_ecs_diagnostics(s, runner=runner)
    paths = list((s.evidence_dir / "diagnostics").glob("ecs-*.json"))
    assert len(paths) == 2
    result = loads(paths[0].read_text())
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["stoppedReason"] == "Task failed container health checks"
    assert not result["collection_errors"]
    assert "secret" not in paths[0].read_text()
    assert "123456789012" not in paths[0].read_text()


def test_inventory_errors_are_retained_without_raw_output(tmp_path):
    s = session(tmp_path)
    capture_ecs_diagnostics(
        s, runner=lambda args: CompletedProcess(args, 1, "", "secret token")
    )
    result = loads(
        next((s.evidence_dir / "diagnostics").glob("ecs-*.json")).read_text()
    )
    assert len(result["collection_errors"]) == 2
    assert not result["tasks"]
    assert "secret" not in dumps(result)
