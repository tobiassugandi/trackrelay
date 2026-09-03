"""Replay the real 11-minute k6 driver against a local-only deduplicating receiver.

No AWS or TrackRelay services are contacted. This checks driver execution, not
application performance or cloud qualification. Requires Docker (Desktop on
macOS, host networking on Linux) and a fresh output directory.
"""

from argparse import ArgumentParser
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from pathlib import Path
from platform import system
from subprocess import Popen, TimeoutExpired, run
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

from trackrelay.aws_fixed_control import derive_ingestion_steps
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    build_elasticity_k6_command,
    prepare_elasticity_workload,
)


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.output_directory.exists():
        parser.error("output directory must be fresh")
    definition = ELASTICITY_WORKLOAD_DEFINITION
    definition_path, manifest_path = prepare_elasticity_workload(
        ElasticityTreatment.FIXED, output_directory=args.output_directory
    )
    manifest = loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        dumps(event["payload"], sort_keys=True): event
        for event in manifest["expected_events"]
    }
    accepted: set[str] = set()
    requests: list[dict[str, object]] = []
    lock = Lock()
    started = monotonic()

    class Receiver(BaseHTTPRequestHandler):
        # Exercise normal keep-alive traffic, not Docker Desktop's per-request
        # connection forwarding limits with the stdlib HTTP/1.0 default.
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            payload = loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = dumps(payload, sort_keys=True)
            event = expected.get(key)
            with lock:
                if (
                    event is None
                    or self.path != f"/api/v1/partners/{event['partner_id']}/events"
                    or self.headers.get("X-Test-Run-ID") != manifest["test_run_id"]
                ):
                    status = 400
                elif key in accepted:
                    status = 200
                else:
                    accepted.add(key)
                    status = 201
                requests.append(
                    {
                        "elapsed_seconds": monotonic() - started,
                        "sequence_number": event["sequence_number"] if event else None,
                        "status": status,
                    }
                )
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 128

    receiver = Server(("127.0.0.1", 0), Receiver)
    thread = Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    host = "127.0.0.1" if system() == "Linux" else "host.docker.internal"
    command = list(
        build_elasticity_k6_command(
            definition,
            manifest_path=manifest_path,
            definition_path=definition_path,
            result_directory=args.output_directory,
            api_url_for_container=f"http://{host}:{receiver.server_port}",
        )
    )
    name = f"trackrelay-driver-check-{uuid4().hex[:12]}"
    command[3:3] = ["--name", name]
    if system() == "Linux":
        command[3:3] = ["--network", "host"]
    (args.output_directory / "k6-command.json").write_text(
        dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    evidence: dict[str, object] = {
        "local_only": True,
        "started_at": datetime.now(UTC).isoformat(),
        "expected_requests": definition.expected_request_count,
        "k6_image": definition.k6_image,
        "driver_sha256": sha256(
            (
                Path(__file__).resolve().parents[1] / "load/elasticity-steps.js"
            ).read_bytes()
        ).hexdigest(),
        "passed": False,
    }
    process = None
    try:
        with (args.output_directory / "k6.log").open("w", encoding="utf-8") as log:
            process = Popen(command, stdout=log, stderr=log, text=True)
            while True:
                try:
                    code = process.wait(timeout=30)
                    break
                except TimeoutExpired:
                    with lock:
                        print(
                            f"Local k6: {monotonic() - started:.0f}s; "
                            f"requests={len(requests)}, unique={len(accepted)}",
                            flush=True,
                        )
                    if monotonic() - started > definition.duration_seconds + 120:
                        raise RuntimeError("local k6 replay exceeded its deadline")
        summary = loads(
            (args.output_directory / "k6-summary.json").read_text(encoding="utf-8")
        )
        steps = derive_ingestion_steps(definition, summary)
        failed_thresholds = [
            f"{name}: {threshold}"
            for name, metric in summary["metrics"].items()
            for threshold, result in metric.get("thresholds", {}).items()
            if not result["ok"]
        ]
        evidence.update(
            k6_exit_code=code,
            failed_thresholds=failed_thresholds,
            ingestion_steps=[step.model_dump() for step in steps],
            passed=(
                code == 0
                and not failed_thresholds
                and len(requests) == len(accepted) == definition.expected_request_count
                and all(request["status"] == 201 for request in requests)
                and all(step.ingestion_guardrails_passed for step in steps)
                and summary["metrics"]["dropped_iterations"]["values"]["count"] == 0
            ),
        )
    finally:
        # Only this invocation's random, named local container is ever removed.
        try:
            cleanup = run(
                ("docker", "container", "rm", "--force", name),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if cleanup.returncode and "No such container" not in cleanup.stderr:
                evidence["passed"] = False
                evidence["cleanup_error"] = cleanup.stderr
        except (OSError, TimeoutExpired) as error:
            evidence["passed"] = False
            evidence["cleanup_error"] = str(error)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)
        evidence.update(
            elapsed_seconds=monotonic() - started,
            observed_requests=len(requests),
            unique_events=len(accepted),
            requests=requests,
        )
        (args.output_directory / "local-driver-result.json").write_text(
            dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
    print(
        f"Local driver {'passed' if evidence['passed'] else 'FAILED'}: "
        f"{len(requests)} requests, {len(accepted)} unique events; "
        f"evidence: {args.output_directory}",
        flush=True,
    )
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
