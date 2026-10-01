from fastapi.testclient import TestClient
from pytest import fixture

from src.server import app


@fixture
def client():
    with TestClient(app, base_url="http://testserver/api/v1") as client:
        yield client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
