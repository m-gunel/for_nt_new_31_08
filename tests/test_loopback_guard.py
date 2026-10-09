"""HTTP API отвечает только запросам с loopback-Host и без чужого Origin.

Проверяется настоящее приложение src.server.app без lifespan: модель не
грузится, а запросы идут в ручки без зависимостей от индекса (/health) или
отбиваются валидацией тела до вызова ручки (/read_file с пустым телом), так
что ни одна ручка не меняет состояние даже без проверки.
"""

import pytest
from fastapi.testclient import TestClient

from src.server import app

PORT = 41234


@pytest.fixture
def client(monkeypatch):
    # conftest разрешает testserver для остальных тестов; здесь — строгое умолчание.
    monkeypatch.delenv("RAG_ALLOWED_HOSTS", raising=False)
    return TestClient(app, base_url="http://127.0.0.1:%d" % PORT)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT, "127.0.0.1"],
)
def test_loopback_host_passes(client, host):
    """Мост и curl: Host — loopback, Origin нет."""
    resp = client.get("/api/v1/health", headers={"host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["evil.example:%d" % PORT, "testserver", "0.0.0.0"])
def test_foreign_host_rejected(client, host):
    """DNS rebinding: браузер шлёт Host с именем сайта атакующего."""
    resp = client.get("/api/v1/health", headers={"host": host})
    assert resp.status_code == 400
    assert "Host" in resp.json()["detail"]


@pytest.mark.parametrize("origin", ["http://evil.example", "null"])
def test_foreign_origin_rejected(client, origin):
    resp = client.get("/api/v1/health", headers={"origin": origin})
    assert resp.status_code == 403


def test_foreign_origin_rejected_before_handler(client):
    """POST со стороннего сайта не доходит даже до валидации тела."""
    resp = client.post(
        "/api/v1/read_file", json={}, headers={"origin": "http://evil.example"}
    )
    assert resp.status_code == 403


def test_swagger_same_origin_still_works(client):
    """Swagger UI с того же адреса: страница, схема и «Try it out»."""
    origin = "http://127.0.0.1:%d" % PORT
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # До ручки доходит: 422 — это уже валидация тела, а не отказ проверки.
    resp = client.post("/api/v1/read_file", json={}, headers={"origin": origin})
    assert resp.status_code == 422
    resp = client.get(
        "/api/v1/health", headers={"origin": "http://localhost:%d" % PORT}
    )
    assert resp.status_code == 200


def test_allowed_hosts_env_extends_list(client, monkeypatch):
    """Явно открытый в сеть демон: свои имена — через RAG_ALLOWED_HOSTS."""
    monkeypatch.setenv("RAG_ALLOWED_HOSTS", "rag.example, Other.Example")
    resp = client.get("/api/v1/health", headers={"host": "rag.example:8000"})
    assert resp.status_code == 200
    resp = client.get("/api/v1/health", headers={"origin": "http://other.example"})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "value",
    [None, "", "evil.example@127.0.0.1", "127.0.0.1:abc", "127.0.0.1:8000/x"],
)
def test_malformed_host_rejected(monkeypatch, value):
    from src.server.loopback_guard import host_allowed

    monkeypatch.delenv("RAG_ALLOWED_HOSTS", raising=False)
    assert host_allowed(value) is False


def test_origin_scheme_must_be_http(monkeypatch):
    from src.server.loopback_guard import origin_allowed

    monkeypatch.delenv("RAG_ALLOWED_HOSTS", raising=False)
    assert origin_allowed("http://127.0.0.1:8000")
    assert origin_allowed("https://localhost")
    assert not origin_allowed("file://")
    assert not origin_allowed("chrome-extension://127.0.0.1")
