"""Старые клиенты после удаления почты: их запросы не ломаются.

Скрипты НТ и прежние клиенты шлют конфиг целиком, с секцией email, а в поиске —
sources. Роутер проверяется на приложении без lifespan: модель и индексатор
не поднимаются.
"""

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.indexer.utils import _get_index
from src.server.router import router
from src.utils.config_mixin import _get_config


class _Index:
    """Заглушка Index: журнал вызовов, достаточный для /config и /search."""

    def __init__(self) -> None:
        self.calls: list = []
        self.active = True

    async def stop_index(self) -> None:
        self.calls.append("stop_index")

    async def start_reindex(self) -> None:
        self.calls.append("start_reindex")

    async def delete_prefix(self, prefix: str) -> None:
        self.calls.append(("delete_prefix", prefix))

    async def search(self, query: str, **kwargs) -> None:
        self.calls.append(("search", query))
        return None


@pytest.fixture
def index(monkeypatch) -> _Index:
    import src.server.router as router_module

    async def _noop() -> None:
        return None

    monkeypatch.setattr(router_module, "stop_watcher", _noop)
    monkeypatch.setattr(router_module, "restart_watcher", _noop)
    return _Index()


@pytest.fixture
def client(user_conf_config, index) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[_get_config] = lambda: user_conf_config
    app.dependency_overrides[_get_index] = lambda: index
    return TestClient(app)


def test_config_body_with_email_is_accepted_but_not_saved(
    client, user_conf_config, tmp_path
):
    docs = tmp_path / "docs"
    docs.mkdir()
    body = {
        "folders": [str(docs)],
        "email": {"enabled": True, "path_to_email_folder": "", "index_last_n_days": 90},
        "sberchat": {"enabled": False},
    }

    response = client.post("/api/v1/config", json=body)

    assert response.status_code == 200
    saved = client.get("/api/v1/config").json()
    assert saved["folders"] == [str(docs)]
    assert "email" not in saved
    assert "email" not in yaml.safe_load(user_conf_config.user_conf_path.read_text())


def test_unknown_config_key_is_still_rejected(client, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    response = client.post("/api/v1/config", json={"folders": [str(docs)], "emial": {}})

    assert response.status_code == 422


def test_search_in_email_only_is_empty(client, index):
    response = client.post(
        "/api/v1/search", json={"query": "отчёт", "sources": ["email"]}
    )

    assert response.status_code == 200
    assert response.json() == {"results": [], "total": 0}
    assert index.calls == []


def test_search_with_files_and_email_searches_files(client, index):
    response = client.post(
        "/api/v1/search", json={"query": "отчёт", "sources": ["files", "email"]}
    )

    assert response.status_code == 200
    assert index.calls == [("search", "отчёт")]


def test_get_file_path_is_gone(client):
    response = client.post(
        "/api/v1/get_file_path", json={"absolute_path": "email_INBOX:0:10:body"}
    )

    assert response.status_code == 404
