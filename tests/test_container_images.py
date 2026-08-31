"""Contract tests for the three production container-image targets."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
MAKEFILE = ROOT / "Makefile"


def target_body(dockerfile: str, target: str) -> str:
    """Return one final target without relying on a Docker parser."""
    marker = f"FROM runtime-base AS {target}\n"
    body = dockerfile.split(marker, 1)[1]
    return body.split("\nFROM runtime-base AS ", 1)[0]


def test_role_targets_have_distinct_runtime_commands() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    worker = target_body(dockerfile, "worker")
    simulator = target_body(dockerfile, "simulator")
    api = target_body(dockerfile, "api")

    assert 'CMD ["trackrelay-worker"]' in worker
    assert "trackrelay.main:app" not in worker
    assert "trackrelay.downstream.main:app" not in worker

    assert "EXPOSE 8001" in simulator
    assert "http://127.0.0.1:8001/health/live" in simulator
    assert '"trackrelay.downstream.main:app"' in simulator

    assert "EXPOSE 8000" in api
    assert "http://127.0.0.1:8000/health/live" in api
    assert '"trackrelay.main:app"' in api


def test_api_remains_the_default_final_target() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert dockerfile.rfind("FROM runtime-base AS api") > dockerfile.rfind(
        "FROM runtime-base AS simulator"
    )
    assert dockerfile.rfind("FROM runtime-base AS api") > dockerfile.rfind(
        "FROM runtime-base AS worker"
    )


def test_makefile_builds_and_smokes_each_named_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    for target in ("api", "worker", "simulator"):
        assert f"--target {target}" in makefile
        assert f"image-{target}-smoke:" in makefile
    assert "images-smoke: image-api-smoke image-worker-smoke " in makefile
