"""Retain bounded, non-secret ECS evidence before destructive cleanup."""

from datetime import UTC, datetime
from hashlib import sha256
from json import dumps, loads
from re import sub

from trackrelay.aws_session import AwsSession, CommandRunner, run_command


def error_evidence(error: BaseException | None) -> dict[str, object] | None:
    """Unwrap controller failures without recording arbitrary exception payloads."""
    return _error_evidence(error, set())


def _error_evidence(
    error: BaseException | None, seen: set[int]
) -> dict[str, object] | None:
    if error is None or id(error) in seen or len(seen) >= 20:
        return None
    seen.add(id(error))
    result: dict[str, object] = {"type": type(error).__name__}
    # These controllers deliberately expose safe action/guardrail descriptions.
    # HTTP/client/subprocess exceptions can contain URLs, credentials or payloads.
    if type(error).__module__.startswith("trackrelay.aws_"):
        result["message"] = str(error)[:2000]
    nested = getattr(error, "workflow_error", None) or error.__cause__
    if nested is not None:
        result["cause"] = _error_evidence(nested, seen)
    cleanup = getattr(error, "cleanup_errors", ())
    if cleanup:
        result["cleanup"] = [_error_evidence(item, seen) for item in cleanup]
    return result


def capture_ecs_diagnostics(
    session: AwsSession, *, runner: CommandRunner = run_command
) -> None:
    """Save stopped/running task reasons before ECS metadata and logs disappear.

    Collection is best effort; neither an AWS failure nor a disk failure may
    prevent the caller's unconditional teardown. No container environment,
    credentials, raw process output, network addresses or account ARNs are saved.
    """
    cluster = f"trackrelay-{sha256(session.session_id.encode()).hexdigest()[:8]}-async"
    prefix = (
        "aws",
        "--profile",
        session.profile,
        "--region",
        session.region,
        "--cli-connect-timeout",
        "5",
        "--cli-read-timeout",
        "10",
    )
    evidence: dict[str, object] = {
        "session_id": session.session_id,
        "collected_at": datetime.now(UTC).isoformat(),
        "tasks": [],
        "collection_errors": [],
    }
    tasks = evidence["tasks"]
    errors = evidence["collection_errors"]
    seen: set[str] = set()
    for status in ("STOPPED", "RUNNING"):
        try:
            result = runner(
                (
                    *prefix,
                    "ecs",
                    "list-tasks",
                    "--cluster",
                    cluster,
                    "--desired-status",
                    status,
                    "--output",
                    "json",
                )
            )
            if result.returncode:
                raise RuntimeError("task listing failed")
            arns = loads(result.stdout)["taskArns"]
            if not isinstance(arns, list) or any(
                not isinstance(arn, str) or f":task/{cluster}/" not in arn
                for arn in arns
            ):
                raise ValueError("invalid task listing")
            # Bound diagnostics independently of the teardown deadline.
            selected = sorted(set(arns) - seen)[:100]
            seen.update(selected)
            if not selected:
                continue
            result = runner(
                (
                    *prefix,
                    "ecs",
                    "describe-tasks",
                    "--cluster",
                    cluster,
                    "--tasks",
                    *selected,
                    "--output",
                    "json",
                )
            )
            if result.returncode:
                raise RuntimeError("task inspection failed")
            document = loads(result.stdout)
            for task in document["tasks"]:
                task = {
                    **task,
                    "stoppedReason": sub(
                        r"arn:[^\s)]+|https?://[^\s)]+",
                        "[resource]",
                        str(task.get("stoppedReason") or "")[:2000],
                    ),
                }
                tasks.append(
                    {
                        key: task.get(key)
                        for key in (
                            "group",
                            "createdAt",
                            "startedAt",
                            "stoppingAt",
                            "stoppedAt",
                            "stopCode",
                            "stoppedReason",
                        )
                    }
                    | {
                        "containers": [
                            {
                                key: container.get(key)
                                for key in (
                                    "name",
                                    "lastStatus",
                                    "healthStatus",
                                    "exitCode",
                                )
                            }
                            for container in task.get("containers", [])
                        ]
                    }
                )
            if document.get("failures"):
                errors.append({"status": status, "type": "TaskInspectionIncomplete"})
        except Exception as error:  # noqa: BLE001 - diagnostics must not block cleanup
            errors.append({"status": status, "type": type(error).__name__})
    root = session.evidence_dir / "diagnostics"
    root.mkdir(parents=True, exist_ok=True)
    # Never replace the original pre-teardown evidence on a cleanup retry.
    destination = root / f"ecs-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    with destination.open("x", encoding="utf-8") as output:
        output.write(dumps(evidence, indent=2) + "\n")
