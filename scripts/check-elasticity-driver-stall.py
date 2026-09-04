"""Local-only v5/v6 driver regression with a 4.3-second receiver stall.

Requires Docker; sends no requests to AWS. This is a 20-second peak-only probe,
not the frozen waveform or a latency/elasticity qualification run.
"""

from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from pathlib import Path
from platform import system
from subprocess import run
from threading import Lock, Thread
from time import monotonic, sleep
from uuid import uuid4

from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    build_elasticity_k6_command,
    prepare_elasticity_workload,
)
from trackrelay.experiments.request_timings import read_request_timings


def probe(root: Path, version: str) -> dict:
    definition = ELASTICITY_WORKLOAD_DEFINITION.model_copy(
        update={"name": f"aws-elasticity-demo-{version}"}
    )
    definition_path, manifest_path = prepare_elasticity_workload(
        ElasticityTreatment.FIXED, output_directory=root, definition=definition
    )
    lock = Lock()
    first_request = None
    accepted = set()

    class Receiver(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            nonlocal first_request
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                if first_request is None:
                    first_request = monotonic()
                elapsed = monotonic() - first_request
                duplicate = payload in accepted
                accepted.add(payload)
            if 5 <= elapsed < 9.3:
                sleep(9.3 - elapsed)
            self.send_response(200 if duplicate else 201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 256
        daemon_threads = True

    receiver = Server(("127.0.0.1", 0), Receiver)
    thread = Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    host = "127.0.0.1" if system() == "Linux" else "host.docker.internal"
    command = list(
        build_elasticity_k6_command(
            definition,
            manifest_path=manifest_path,
            definition_path=definition_path,
            result_directory=root,
            api_url_for_container=f"http://{host}:{receiver.server_port}",
        )
    )
    name = f"trackrelay-stall-probe-{uuid4().hex[:12]}"
    command[3:3] = ["--name", name]
    if system() == "Linux":
        command[3:3] = ["--network", "host"]
    command[-1] = "/scripts/elasticity-stall-probe.js"
    try:
        with (root / "k6.log").open("w") as log:
            process = run(command, stdout=log, stderr=log, timeout=60, check=False)
        summary = loads((root / "k6-summary.json").read_text())["metrics"]
        samples = read_request_timings(root / "k6-points.json")
        return {
            "version": version,
            "k6_exit": process.returncode,
            "requests": summary["http_reqs"]["values"]["count"],
            "unique_requests": len(accepted),
            "dropped_iterations": summary.get(
                "dropped_iterations", {"values": {"count": 0}}
            )["values"]["count"],
            "raw_samples": len(samples),
            "maximum_latency_ms": max(item.duration_ms for item in samples),
        }
    finally:
        try:
            run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=15,
                check=False,
            )
        finally:
            receiver.shutdown()
            receiver.server_close()
            thread.join(timeout=5)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.output_directory.exists():
        parser.error("output directory must be fresh")
    results = [
        probe(args.output_directory / version, version) for version in ("v5", "v6")
    ]
    old, new = results
    passed = (
        old["k6_exit"] == 99
        and old["dropped_iterations"] > 0
        and new["k6_exit"] == 0
        and new["dropped_iterations"] == 0
        and new["requests"] == new["unique_requests"] == new["raw_samples"] == 500
        and all(result["maximum_latency_ms"] >= 3500 for result in results)
    )
    evidence = {
        "local_only": True,
        "qualification_run": False,
        "passed": passed,
        "results": results,
    }
    (args.output_directory / "result.json").write_text(dumps(evidence, indent=2) + "\n")
    print(dumps(evidence, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
