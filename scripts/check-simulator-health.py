"""Soak a disposable simulator at its frozen Fargate CPU/memory limits.

This local diagnostic is not cloud qualification evidence. Both existing and
rebuilt images can be tested; the image's actual HEALTHCHECK is exercised.
"""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from json import dumps, loads
from pathlib import Path
from subprocess import run
from time import monotonic, sleep
from uuid import uuid4

import httpx


def docker(*args: str) -> str:
    result = run(
        ("docker", *args), capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr}")
    return result.stdout.strip()


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--rate", type=int, default=10)
    args = parser.parse_args()
    if (
        args.output.exists()
        or not 1 <= args.duration_seconds <= 600
        or not 1 <= args.rate <= 25
    ):
        parser.error("use a fresh output, duration 1..600 seconds and rate 1..25")
    name = f"trackrelay-simulator-health-{uuid4().hex[:12]}"
    evidence = {
        "image": args.image,
        "cpus": 0.25,
        "memory_mib": 512,
        "started_at": datetime.now(UTC).isoformat(),
        "health_samples": [],
        "duration_seconds": args.duration_seconds,
        "rate": args.rate,
        "passed": False,
    }
    image = loads(docker("image", "inspect", args.image))[0]
    evidence["image_id"] = image["Id"]
    evidence["architecture"] = image["Architecture"]
    evidence["healthcheck"] = image["Config"].get("Healthcheck")
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--cpus",
            "0.25",
            "--memory",
            "512m",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--init",
            "--publish",
            "127.0.0.1::8001",
            args.image,
        )
        address = docker("port", name, "8001/tcp")
        base_url = f"http://{address}"
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            deadline = monotonic() + 90
            while True:
                health = loads(
                    docker("inspect", "--format", "{{json .State.Health}}", name)
                )
                if health["Status"] == "healthy":
                    break
                if health["Status"] == "unhealthy" or monotonic() >= deadline:
                    raise RuntimeError("simulator failed initial health check")
                sleep(1)

            def send(index):
                response = client.post(
                    "/events",
                    json={
                        "partner_id": "health-soak",
                        "partner_event_id": str(index),
                        "tracking_number": f"HEALTH-{index}",
                        "status": "picked_up",
                        "occurred_at": evidence["started_at"],
                        "received_at": evidence["started_at"],
                        "raw_payload": {},
                    },
                )
                return response.status_code

            started = monotonic()
            next_health = started
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = []
                for index in range(args.duration_seconds * args.rate):
                    sleep(max(0, started + index / args.rate - monotonic()))
                    if sum(not future.done() for future in futures) >= 32:
                        raise RuntimeError("local request concurrency limit exceeded")
                    futures.append(pool.submit(send, index))
                    if monotonic() >= next_health:
                        health = loads(
                            docker(
                                "inspect", "--format", "{{json .State.Health}}", name
                            )
                        )
                        evidence["health_samples"].append(health)
                        print(
                            f"{monotonic() - started:.0f}s: health={health['Status']}",
                            flush=True,
                        )
                        next_health = monotonic() + 10
                statuses = [future.result() for future in futures]
            evidence["accepted"] = statuses.count(202)
            evidence["expected"] = len(statuses)
            count = client.get("/control/events/count")
            count.raise_for_status()
            evidence["receipts"] = count.json()["event_count"]
            final_health = loads(
                docker("inspect", "--format", "{{json .State.Health}}", name)
            )
            evidence["health_samples"].append(final_health)
            evidence["passed"] = evidence["accepted"] == evidence[
                "expected"
            ] == evidence["receipts"] and all(
                sample["Status"] == "healthy"
                and sample["FailingStreak"] == 0
                and all(entry["ExitCode"] == 0 for entry in sample["Log"])
                for sample in evidence["health_samples"]
            )
    finally:
        try:
            docker("container", "rm", "--force", name)
        finally:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(dumps(evidence, indent=2) + "\n")
    print(f"Simulator health soak passed: {evidence['passed']}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
