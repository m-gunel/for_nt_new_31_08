"""Ручки управления индексацией — БТ Библиотеки §5.1-5.3.

Роутерная часть проверяется прямым вызовом хендлеров с подставным индексом:
полный lifespan (OpenVINO + LanceDB + watcher) для проверки оркестрации не
нужен. Состояние самого индекса проверяется на реальном Index из фикстуры
изолированного хранилища.
"""

import asyncio
from contextlib import suppress

import pytest

from src.config import Config
from src.indexer.indexer import Index
from src.server.model import UserConfig
from src.server.router import (
    activate_indexing,
    deactivate_indexing,
    reset_indexing,
    update_config,
)
from src.utils.lsm_adapter import to_thread as lsm_to_thread

# Фикстуры поднимают Index на временном хранилище — переиспользуем их.
from tests.test_indexing_live import index, isolated_config  # noqa: F401


class ControlIndex:
    """Протокол Index, достаточный для ручек управления и update_config."""

    def __init__(self, active: bool = True):
        self.calls: list = []
        self.emails_scanner = None
        self.active = active

    async def activate(self) -> bool:
        self.calls.append("activate")
        changed = not self.active
        self.active = True
        return changed

    async def deactivate(self) -> bool:
        self.calls.append("deactivate")
        changed = self.active
        self.active = False
        return changed

    async def reset_index(self) -> None:
        self.calls.append("reset_index")
        self.active = False

    async def stop_index(self):
        self.calls.append("stop_index")

    async def start_reindex(self):
        self.calls.append("start_reindex")

    async def delete_prefix(self, prefix):
        self.calls.append(("delete_prefix", prefix))

    async def drop_email_index(self):
        self.calls.append("drop_email_index")

    async def start_emails_polling(self, immediate=False):
        self.calls.append(("start_emails_polling", immediate))

    async def stop_emails_polling(self):
        self.calls.append("stop_emails_polling")


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


@pytest.fixture
def tmp_config(config: Config, tmp_path, monkeypatch):
    """Конфиг, чей user_conf.yml лежит во временном каталоге.

    cached_property снимается и на входе, и на ВЫХОДЕ: monkeypatch вернёт
    data_directory, а закешированные под временный каталог пути остались бы
    в объекте и протекли бы в следующий тест.
    """
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    yield config
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


# --------------------------------------------------------------------------
# §5.1 Activation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activate_starts_indexing_and_watcher(patched_watcher):
    idx = ControlIndex(active=False)
    result = await activate_indexing(idx)
    assert result.active is True
    assert result.changed is True
    assert "activate" in idx.calls
    # Наблюдение обязано подняться вместе с индексацией: §5.2 считает
    # обработку новых файлов частью индексации.
    assert "restart_watcher" in patched_watcher


@pytest.mark.asyncio
async def test_activate_is_idempotent(patched_watcher):
    """Повторная активация не считается изменением и не рвёт идущий проход."""
    idx = ControlIndex(active=True)
    result = await activate_indexing(idx)
    assert result.active is True
    assert result.changed is False


# --------------------------------------------------------------------------
# §5.2 Deactivation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deactivate_stops_scanning_and_keeps_index(patched_watcher):
    idx = ControlIndex(active=True)
    result = await deactivate_indexing(idx)
    assert result.active is False
    assert result.changed is True
    assert "stop_watcher" in patched_watcher
    # §5.2: индексы НЕ удаляются — ни дропа таблиц, ни сброса состояния.
    assert "reset_index" not in idx.calls
    assert "drop_email_index" not in idx.calls


@pytest.mark.asyncio
async def test_deactivate_is_idempotent(patched_watcher):
    idx = ControlIndex(active=False)
    result = await deactivate_indexing(idx)
    assert result.active is False
    assert result.changed is False


# --------------------------------------------------------------------------
# §5.3 Reset
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_clears_index_and_user_config(tmp_config, patched_watcher):
    tmp_config.update_user_config(UserConfig.model_validate({"folders": None}))
    assert tmp_config.user_conf_path.exists()

    idx = ControlIndex(active=True)
    result = await reset_indexing(idx, tmp_config)

    assert "stop_watcher" in patched_watcher
    assert "reset_index" in idx.calls
    # §5.3 «как после установки» — то же, что делает RESET=1 на старте:
    # пользовательских настроек тоже не остаётся.
    assert not tmp_config.user_conf_path.exists()
    # Сброс не запускает обход заново: §5.3 применяется в том числе при смене
    # пользователя, когда лезть в файлы до согласия нельзя.
    assert result.active is False


@pytest.mark.asyncio
async def test_reset_can_keep_user_config(tmp_config, patched_watcher):
    tmp_config.update_user_config(UserConfig.model_validate({"folders": None}))
    idx = ControlIndex(active=True)

    await reset_indexing(idx, tmp_config, keep_config=True)

    assert tmp_config.user_conf_path.exists(), "keep_config=true не сохранил конфиг"


# --------------------------------------------------------------------------
# §5.4 Смена списка папок при остановленной индексации
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_config_change_does_not_scan_while_deactivated(
    tmp_config, patched_watcher, tmp_path
):
    """§5.4: папка добавляется в список, но сканирование не начинается."""
    docs = tmp_path / "docs"
    docs.mkdir()
    idx = ControlIndex(active=False)

    await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}), tmp_config, idx
    )

    saved = tmp_config.user_config
    assert saved is not None and saved.folders == [str(docs)], "список не сохранён"
    assert "start_reindex" not in idx.calls, "сканирование пошло на выключенном индексе"
    assert "restart_watcher" not in patched_watcher, "watcher поднят вопреки §5.2"


@pytest.mark.asyncio
async def test_config_change_scans_when_active(tmp_config, patched_watcher, tmp_path):
    """Обратная сторона: при активной индексации поведение прежнее."""
    docs = tmp_path / "docs"
    docs.mkdir()
    idx = ControlIndex(active=True)

    await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}), tmp_config, idx
    )

    assert "start_reindex" in idx.calls
    assert "restart_watcher" in patched_watcher


# --------------------------------------------------------------------------
# Состояние реального индекса
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_reports_active_flag(index: Index):  # noqa: F811
    """`active` отличает «остановлено» от «простаиваю» — оба дают is_indexing=false."""
    assert (await index.status()).active is True

    assert await index.deactivate() is True
    status = await index.status()
    assert status.active is False
    assert status.is_indexing is False

    # Повторная деактивация состояния не меняет.
    assert await index.deactivate() is False


@pytest.mark.asyncio
async def test_activate_sets_flag_back(index: Index):  # noqa: F811
    await index.deactivate()
    assert await index.activate() is True
    assert index.active is True
    # Задача прохода создана — снимаем её, чтобы не течь за пределы теста.
    await index.deactivate()


@pytest.mark.asyncio
async def test_deactivate_makes_status_honest(index: Index):  # noqa: F811
    """После остановки /status не должен рапортовать «индексация идёт».

    is_indexing считается по `pending > 0`, а в счётчике после отмены прохода
    остаётся хвост очереди. Без явного обнуления остановленный индексатор
    вечно показывал бы работу — и MCP-мост говорил бы агенту, что пустая
    выдача не окончательна.
    """
    index.counter.reset(5)
    index.watch_pending = 3
    assert (await index.status()).is_indexing is True

    await index.deactivate()

    status = await index.status()
    assert status.is_indexing is False, "остановленный индексатор считает себя занятым"
    assert status.active is False
    assert status.files_to_index == 0


@pytest.mark.asyncio
async def test_activate_does_not_restart_live_email_polling(
    index: Index,  # noqa: F811
    monkeypatch,
):
    """Повторная активация не обрывает идущий почтовый проход.

    start_emails_polling внутри себя зовёт stop_emails_polling, поэтому
    безусловный вызов отменял бы задачу прямо посреди чтения mbox.
    """
    calls: list = []

    async def spy(immediate: bool = False):
        calls.append(immediate)

    async def never_ending():
        await asyncio.Event().wait()

    monkeypatch.setattr(index, "start_emails_polling", spy)
    index.emails_task = asyncio.create_task(never_ending())
    try:
        await index.activate()
        assert calls == [], "живой поллинг почты перезапущен"
    finally:
        index.emails_task.cancel()
        with suppress(asyncio.CancelledError):
            await index.emails_task
        index.emails_task = None
        await index.deactivate()


@pytest.mark.asyncio
async def test_preempted_full_pass_is_replayed(index: Index):  # noqa: F811
    """Полный проход, вытесненный почтовым, не теряется, а доигрывается."""
    await index._scan_lock.acquire()
    try:
        await index.scan_all()  # файловый проход упирается в занятый лок
        assert index._rescan_requested is True
    finally:
        index._scan_lock.release()

    # Почтовый проход флага не ставит: его повторит поллинг.
    index._rescan_requested = False
    await index._scan_lock.acquire()
    try:
        await index.scan_all(emails_only=True)
        assert index._rescan_requested is False
    finally:
        index._scan_lock.release()


@pytest.mark.asyncio
async def test_reset_index_clears_storage(index: Index):  # noqa: F811
    """Сброс на реальном индексе: состояние LSM пусто, индексация выключена."""
    from src.common import FileMetadata

    await lsm_to_thread(
        index.lsm_collection.indexed.set,
        "/tmp/пример.txt",
        FileMetadata(path="/tmp/пример.txt", size=1, mtime_seconds=1),
    )
    assert index.lsm_collection.indexed.keys_snapshot(), "нечего сбрасывать"

    await index.reset_index()

    assert index.lsm_collection.indexed.keys_snapshot() == [], "состояние LSM осталось"
    assert index.active is False, "после сброса индексация должна быть выключена"
    assert (await index.status()).is_indexing is False
    # Таблица должна быть снята с учёта, а не остаться в кеше хендла.
    assert index.files_store.has_table is False
