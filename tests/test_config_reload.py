"""POST /config: порядок шагов и снятие папок с индекса.

Фоновая задача `start_reindex()` живёт вне `reconfigure_lock`, поэтому
стартует последним шагом: параллельно со снятием папок (`delete_prefix()`)
она писала бы в ту же БД состояния (assert_db_state в lsm1, SIGABRT).
"""

import os

import pytest

from src.config import Config
from src.indexer.indexer import Index
from src.server.model import UserConfig, UserConfigUpdate
from src.server.router import update_config

# Index на временном хранилище.
from tests.test_indexing_live import index, isolated_config  # noqa: F401


@pytest.fixture
def cfg(config: Config, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    yield config
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pytest.fixture
def journal(monkeypatch):
    """Единый журнал вызовов индексатора и watcher'а — важен их порядок."""
    import src.server.router as router_module

    calls: list = []

    async def fake_stop():
        calls.append("stop_watcher")

    async def fake_restart():
        calls.append("restart_watcher")

    monkeypatch.setattr(router_module, "stop_watcher", fake_stop)
    monkeypatch.setattr(router_module, "restart_watcher", fake_restart)
    return calls


class RecordingIndex:
    """Протокол Index, достаточный для update_config, с общим журналом."""

    def __init__(self, calls: list):
        self.calls = calls
        # update_config не сканирует на деактивированном индексе (БТ §5.4);
        # True — как у демона с автозапуском.
        self.active = True

    async def stop_index(self):
        self.calls.append("stop_index")

    async def start_reindex(self):
        self.calls.append("start_reindex")

    async def delete_prefix(self, prefix):
        self.calls.append(("delete_prefix", prefix))


def _nt_body(folder: str) -> UserConfigUpdate:
    """Тело, которое реально шлёт нагрузочный стенд (и hunt_crash.py).

    Секция email в нём осталась: роутер принимает её и игнорирует.
    """
    return UserConfigUpdate.model_validate(
        {
            "folders": [folder],
            "email": {
                "enabled": False,
                "path_to_email_folder": "",
                "index_last_n_days": 90,
            },
            "sberchat": {"enabled": False},
        }
    )


@pytest.mark.asyncio
async def test_reindex_starts_last(cfg, journal, tmp_path):
    """Перезапуск индексации — последним шагом, после остановки и снятия папок."""
    docs = tmp_path / "docs"
    docs.mkdir()
    fake_index = RecordingIndex(journal)

    await update_config(_nt_body(str(docs)), cfg, fake_index)

    assert "start_reindex" in journal, "переиндексация должна была стартовать"
    assert "restart_watcher" in journal

    # Всё, что трогает состояние индекса, обязано завершиться до старта
    # фоновой задачи.
    start = journal.index("start_reindex")
    for step in ("stop_watcher", "stop_index"):
        assert journal.index(step) < start, f"{step} должен идти до start_reindex"
    assert journal.index("restart_watcher") > start


@pytest.mark.asyncio
async def test_change_without_folders_does_not_restart_indexing(cfg, journal):
    """Запрос без folders не трогает ни индексацию, ни watcher."""
    fake_index = RecordingIndex(journal)

    await update_config(
        UserConfig.model_validate({"sberchat": {"enabled": False}}), cfg, fake_index
    )

    assert "start_reindex" not in journal
    assert "restart_watcher" not in journal
    assert "stop_watcher" not in journal


@pytest.mark.asyncio
async def test_repeated_config_keeps_order_stable(cfg, journal, tmp_path):
    """Повторные POST /config (стенд шлёт их пачкой) не ломают порядок."""
    docs = tmp_path / "docs"
    docs.mkdir()
    fake_index = RecordingIndex(journal)

    for _ in range(10):
        journal.clear()
        await update_config(_nt_body(str(docs)), cfg, fake_index)
        start = journal.index("start_reindex")
        assert journal.index("stop_index") < start
        assert journal.index("restart_watcher") > start


@pytest.mark.asyncio
async def test_delete_prefix_survives_empty_state_db(
    index: Index,  # noqa: F811
):
    """Снятие префикса на пустой таблице состояния не падает.

    На пустой базе курсор lsm не открывается (`RuntimeError: Invalid cursor`),
    а пустая таблица штатна: свежая установка, очищенный индекс, первый
    POST /config.
    """
    assert not list(index.lsm_collection.indexed.items()), "таблица должна быть пуста"

    await index.delete_prefix("/nonexistent/dir/")


@pytest.mark.asyncio
async def test_removed_folder_is_deleted_with_trailing_separator(
    cfg, journal, tmp_path
):
    """Убранная из конфига папка снимается со слэшем на конце.

    delete_prefix удаляет всё, что начинается со строки: без слэша снятие
    ~/Документы задело бы и ~/Документы_архив. Здесь проверяется место вызова
    в router, семантику delete_prefix — соседний тест.
    """
    docs = tmp_path / "Документы"
    archive = tmp_path / "Документы_архив"
    for d in (docs, archive):
        d.mkdir()

    fake_index = RecordingIndex(journal)
    # Сначала сохраняем обе папки: без этого prev_paths пуст и удалять нечего.
    await update_config(
        UserConfig.model_validate({"folders": [str(docs), str(archive)]}),
        cfg,
        fake_index,
    )
    journal.clear()

    # Теперь убираем только Документы.
    await update_config(
        UserConfig.model_validate({"folders": [str(archive)]}), cfg, fake_index
    )

    # В журнале вперемешку строки ("stop_watcher") и кортежи — фильтруем по типу.
    deleted = [
        entry[1]
        for entry in journal
        if isinstance(entry, tuple) and entry[0] == "delete_prefix"
    ]
    assert deleted == [str(docs) + os.sep], (
        f"ожидался ровно один префикс, и со слэшем на конце; получено: {deleted}"
    )


@pytest.mark.asyncio
async def test_delete_prefix_removes_only_its_own_subtree(
    index: Index,  # noqa: F811
    tmp_path,
):
    """Удаляется ровно поддерево префикса, соседи по имени — нет."""
    from src.common import FileMetadata

    table = index.lsm_collection.indexed
    paths = ["/home/a/1.txt", "/home/a/2.txt", "/home/ab/3.txt", "/home/b/4.txt"]
    for p in paths:
        await table.async_set(p, FileMetadata(path=p, size=1, mtime_seconds=1))

    await index.delete_prefix("/home/a/")

    left = sorted(key for key, _ in table.items())
    assert left == ["/home/ab/3.txt", "/home/b/4.txt"], (
        "снято должно быть только /home/a/, но не /home/ab/"
    )


@pytest.mark.asyncio
async def test_folder_change_restarts_watcher_with_real_index(
    cfg,
    journal,
    index: Index,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Смена папки на настоящем Index, а не заглушке.

    Заглушка delete_prefix скрыла бы его поломку — 500 на каждый POST /config
    со сменой папки. Проверяются код ответа и подъём watcher'а.
    """
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    for d in (old_dir, new_dir):
        d.mkdir()

    # Полный проход поднимать не надо — важна только ветка смены папок.
    async def fake_reindex():
        journal.append("start_reindex")

    monkeypatch.setattr(index, "start_reindex", fake_reindex)

    cfg.update_user_config(_nt_body(str(old_dir)))
    journal.clear()

    response = await update_config(_nt_body(str(new_dir)), cfg, index)

    assert response.status_code == 200
    assert "restart_watcher" in journal, (
        f"watcher обязан подняться обратно, журнал: {journal}"
    )
    assert journal.index("stop_watcher") < journal.index("restart_watcher")
