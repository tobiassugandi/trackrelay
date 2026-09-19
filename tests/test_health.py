"""Tests for the API health endpoints."""

from collections.abc import Iterator

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.main import app, database_is_ready


@fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_liveness(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert False, "Intentional failure for the CI learning exercise"


def test_readiness_when_database_is_available(client: TestClient) -> None:
    app.dependency_overrides[database_is_ready] = lambda: True

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_when_database_is_unavailable(client: TestClient) -> None:
    app.dependency_overrides[database_is_ready] = lambda: False

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
