"""Tests for the API health endpoints."""

from fastapi.testclient import TestClient

from trackrelay.main import app


def test_liveness() -> None:
    client = TestClient(app)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
