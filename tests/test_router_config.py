"""Юнит-тесты POST /config — без подъёма всего приложения.

Хендлер вызывается напрямую с ручной подстановкой зависимостей: полный
lifespan (OpenVINO + LanceDB + watcher) для этих проверок не нужен.
"""

import pytest
from fastapi import HTTPException

from src.server.model import SberchatConfig, UserConfig, UserConfigUpdate
from src.server.router import _merge_user_config, update_config


def _current() -> UserConfig:
    return UserConfig(
        folders=["/home/u/docs"],
        max_depth=3,
        sberchat=SberchatConfig(enabled=True),
    )


def test_merge_folders_only_keeps_rest():
    """POST {"folders": [...]} не сбрасывает остальные настройки в дефолт."""
    incoming = UserConfigUpdate.model_validate({"folders": ["/home/u/other"]})

    merged = _merge_user_config(_current(), incoming)

    assert merged.folders == ["/home/u/other"]
    assert merged.max_depth == 3
    assert merged.sberchat.enabled is True


def test_merge_partial_sberchat_keeps_rest():
    incoming = UserConfigUpdate.model_validate({"sberchat": {"enabled": False}})

    merged = _merge_user_config(_current(), incoming)

    assert merged.sberchat.enabled is False
    assert merged.folders == ["/home/u/docs"]
    assert merged.max_depth == 3


def test_merge_full_replacement_still_works():
    incoming = UserConfigUpdate.model_validate(
        {"folders": ["/a"], "max_depth": 1, "sberchat": {"enabled": False}}
    )

    merged = _merge_user_config(_current(), incoming)

    assert merged == UserConfig(
        folders=["/a"], max_depth=1, sberchat=SberchatConfig(enabled=False)
    )


class FakeIndex:
    """Протокол Index, достаточный для update_config; пишет журнал вызовов."""

    def __init__(self):
        self.calls: list = []
        # update_config не сканирует на деактивированном индексе (БТ §5.4);
        # True — как у демона с автозапуском.
        self.active = True

    async def stop_index(self):
        self.calls.append("stop_index")

    async def start_reindex(self):
        self.calls.append("start_reindex")

    async def delete_prefix(self, prefix):
        self.calls.append(("delete_prefix", prefix))


@pytest.fixture
def patched_watcher(monkeypatch):
    import src.server.router as router_module

    calls: list = []

    async def fake_stop():
        calls.append("stop_watcher")

    async def fake_restart():
        calls.append("restart_watcher")

    monkeypatch.setattr(router_module, "stop_watcher", fake_stop)
    monkeypatch.setattr(router_module, "restart_watcher", fake_restart)
    return calls


@pytest.mark.asyncio
async def test_folders_update_keeps_saved_settings(
    user_conf_config, patched_watcher, tmp_path
):
    """Смена папок через ручку не затирает сохранённые глубину и sberchat."""
    docs = tmp_path / "docs"
    docs.mkdir()
    user_conf_config.update_user_config(_current())

    await update_config(
        UserConfigUpdate.model_validate({"folders": [str(docs)]}),
        user_conf_config,
        FakeIndex(),
    )

    saved = user_conf_config.user_config
    assert saved is not None
    assert saved.max_depth == 3
    assert saved.sberchat.enabled is True


@pytest.mark.asyncio
async def test_config_rejects_missing_folder(user_conf_config, patched_watcher):
    """Несуществующая папка отклоняется, и конфиг на диске не меняется.

    Swagger по умолчанию шлёт folders: ["string"], а сохранённый
    несуществующий путь ронял бы watcher на inotify целиком.
    """
    index = FakeIndex()
    before = user_conf_config.user_config

    with pytest.raises(HTTPException) as exc:
        await update_config(
            UserConfigUpdate.model_validate({"folders": ["string"]}),
            user_conf_config,
            index,
        )

    assert exc.value.status_code == 400
    assert "string" in str(exc.value.detail)
    assert user_conf_config.user_config == before
    assert index.calls == [], "переиндексация не должна была стартовать"


@pytest.mark.asyncio
async def test_config_accepts_existing_folder(
    user_conf_config, patched_watcher, tmp_path
):
    docs = tmp_path / "docs"
    docs.mkdir()

    response = await update_config(
        UserConfigUpdate.model_validate({"folders": [str(docs)]}),
        user_conf_config,
        FakeIndex(),
    )

    assert response.status_code == 200
    saved = user_conf_config.user_config
    assert saved is not None and saved.folders == [str(docs)]
