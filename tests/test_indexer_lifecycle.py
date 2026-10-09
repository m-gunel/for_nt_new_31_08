"""Остановка и перезапуск фоновых задач индексатора.

Проход живёт в одной из трёх задач (self.task, rescan_task, emails_task),
почтовый этап — внутри любой из них. Проверки — на настоящем Index во
временном хранилище; ручки роутера зовутся напрямую, watcher подменён.
"""

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio
from pytest import fixture

import src.indexer.scanners.email_scanner as email_scanner_module
import src.indexer.scanners.files_scanner as files_scanner_module
import src.parsers as parsers_module
from src.config import Config, EmailConfig
from src.indexer.indexer import Index, _await_stopped
from src.indexer.scanners.email_scanner import FolderState
from src.server.model import UserConfig
from src.server.router import (
    activate_indexing,
    deactivate_indexing,
    reset_indexing,
    update_config,
)
from src.utils.lsm_adapter import to_thread as lsm_to_thread
from tests.mbox_fixture import default_inbox_messages, make_plain_email, make_profile

_TICK = 0.01


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_TICK)
    return bool(predicate())


async def _failed_task(exc: Exception) -> asyncio.Task:
    """Фоновая задача, упавшая до того, как её стали останавливать."""

    async def boom():
        raise exc

    task = asyncio.create_task(boom())
    await asyncio.wait({task})
    return task


async def _fail_passes(index: Index) -> asyncio.Task:
    """Проход падает так же, как в бою: scan_all логирует ошибку и пробрасывает."""

    async def boom(emails_only: bool = False):
        raise RuntimeError("Infer Request is busy")

    index._scan_all = boom  # type: ignore[method-assign]
    await index.start_reindex()
    await asyncio.wait({index.task})
    assert isinstance(index.task.exception(), RuntimeError)
    return index.task


def _rows(store, prefix: str) -> int:
    return store.count_matching(f"starts_with(path, '{prefix}')")


async def _lsm_keys(index: Index, prefix: str) -> list[str]:
    keys = await lsm_to_thread(index.lsm_collection.indexed.keys_snapshot)
    return [k for k in keys if k.startswith(prefix)]


@fixture
def cfg(config: Config, tmp_path, monkeypatch):
    """Хранилище и user_conf.yml — во временном каталоге, почта выключена."""
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config, "email", EmailConfig(enabled=False))
    monkeypatch.setattr(config.file_scan_cfg, "paths", [])
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)
    yield config
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)


@fixture
def mail_root(tmp_path):
    return make_profile(tmp_path / "tb", {"INBOX": default_inbox_messages()})


@fixture
def email_cfg(cfg: Config, mail_root, monkeypatch):
    monkeypatch.setattr(
        cfg, "email", EmailConfig(enabled=True, path_to_email_folder=str(mail_root))
    )
    return cfg


@fixture
def no_watcher(monkeypatch):
    import src.server.router as router_module

    async def nothing():
        pass

    monkeypatch.setattr(router_module, "stop_watcher", nothing)
    monkeypatch.setattr(router_module, "restart_watcher", nothing)


async def _serve(cfg: Config):
    idx = Index(cfg)
    async with idx:
        try:
            yield idx
        finally:
            # Ни одна фоновая задача теста не переживает индекс.
            await idx.deactivate()


@pytest_asyncio.fixture
async def index(cfg: Config):
    async for idx in _serve(cfg):
        yield idx


@pytest_asyncio.fixture
async def email_index(email_cfg: Config):
    async for idx in _serve(email_cfg):
        yield idx


# --- T008: остановка не поднимает ошибку упавшей задачи ---------------------


@pytest.mark.asyncio
async def test_stop_index_survives_failed_pass(index: Index):
    """Упавший проход не ломает ни первую, ни повторную остановку.

    Ошибка прохода уже в логе; поднятая из stop_index(), она отвечала бы 500
    на /config, /deactivate и /reset до рестарта: упавшая задача так и лежит
    в self.task.
    """
    await _fail_passes(index)

    await index.stop_index()
    await index.stop_index()


@pytest.mark.asyncio
async def test_stop_emails_polling_survives_failed_task(index: Index):
    index.emails_task = await _failed_task(RuntimeError("запись в Lance"))

    await index.stop_emails_polling()

    assert index.emails_task is None


@pytest.mark.asyncio
async def test_control_endpoints_work_after_failed_pass(
    cfg: Config, index: Index, no_watcher, tmp_path
):
    """После упавшего прохода ручки управления отвечают 200 и доделывают своё.

    Иначе POST /config успевал записать конфиг и падал до delete_prefix:
    повтор видел prev_paths == new_paths, и снятая папка оставалась в
    индексе навсегда.
    """
    kept, removed = tmp_path / "A", tmp_path / "B"
    for d in (kept, removed):
        d.mkdir()
    cfg.update_user_config(UserConfig(folders=[str(kept), str(removed)]))
    deleted: list[str] = []
    real_delete = index.delete_prefix

    async def spy(prefix: str) -> None:
        deleted.append(prefix)
        await real_delete(prefix)

    index.delete_prefix = spy  # type: ignore[method-assign]
    await _fail_passes(index)

    response = await update_config(UserConfig(folders=[str(kept)]), cfg, index)

    assert response.status_code == 200
    assert deleted == [str(removed) + os.sep], "снятая папка не снята с индекса"

    # start_reindex() внутри /config снова упал — /deactivate и /reset на нём.
    await asyncio.wait({index.task})
    assert (await deactivate_indexing(index)).active is False
    assert (await reset_indexing(index, cfg, keep_config=True)).changed is True
    assert not index.files_store.has_table, "сброс не дошёл до дропа таблиц"


@pytest.mark.asyncio
async def test_activate_works_after_failed_email_polling(
    email_index: Index, no_watcher
):
    """/activate поднимает поллинг заново, даже если прежний упал."""

    async def nothing(emails_only: bool = False):
        return None

    email_index._scan_all = nothing  # type: ignore[method-assign]
    dead = await _failed_task(RuntimeError("поллинг упал"))
    email_index.emails_task = dead

    result = await activate_indexing(email_index)

    assert result.active is True
    assert email_index.emails_task is not dead
    assert not email_index.emails_task.done(), "поллинг почты не поднят"


@pytest.mark.asyncio
async def test_lifespan_exit_survives_failed_pass(cfg: Config, monkeypatch):
    """Остановка демона не вылетает исключением упавшего прохода."""
    monkeypatch.setattr(cfg, "autostart_indexing", False)
    index = Index(cfg)
    async with index.lifespan():
        await _fail_passes(index)


# --- T032: поллинг почты переживает сбой прохода ----------------------------


@pytest.mark.asyncio
async def test_email_polling_survives_failed_pass(email_index: Index, monkeypatch):
    """Сбой почтового прохода не убивает поллинг: следующий цикл пробует снова.

    Поднять задачу заново некому, а упавшая ломала бы остановку (T008).
    """
    monkeypatch.setattr(email_index.cfg, "email_poll_interval", _TICK)
    calls: list[bool] = []

    async def flaky(emails_only: bool = False) -> None:
        calls.append(emails_only)
        if len(calls) == 1:
            raise RuntimeError("запись в Lance не удалась")

    email_index.scan_all = flaky  # type: ignore[method-assign]

    await email_index.start_emails_polling(immediate=True)

    assert await _wait_until(lambda: len(calls) >= 2), (
        "после упавшего прохода поллинг не сделал следующий"
    )
    assert not email_index.emails_task.done()


# --- T013: остановка гасит и идущий проход, и почтовый этап -----------------


@pytest.mark.asyncio
async def test_removed_folder_is_not_reindexed_by_running_periodic_pass(
    cfg: Config, index: Index, no_watcher, tmp_path, monkeypatch
):
    """Снятая папка не возвращается в индекс идущим периодическим проходом.

    Периодический проход обходит список папок, взятый на старте, и после
    delete_prefix записал бы снятую папку обратно — навсегда.
    """
    kept, removed = tmp_path / "A", tmp_path / "B"
    for d in (kept, removed):
        d.mkdir()
        for i in range(3):
            (d / f"doc{i}.txt").write_text(
                f"Документ {d.name} номер {i}. " * 20, encoding="utf-8"
            )
    cfg.update_user_config(UserConfig(folders=[str(kept), str(removed)]))
    await index.scan_all()
    removed_prefix = str(removed) + os.sep
    assert _rows(index.files_store, removed_prefix) > 0

    real_iter = files_scanner_module.iter_file_metadata

    def slow_iter(*args, **kwargs):
        for meta in real_iter(*args, **kwargs):
            time.sleep(0.2)
            yield meta

    monkeypatch.setattr(files_scanner_module, "iter_file_metadata", slow_iter)
    # Как run_periodic_rescan: полный проход в rescan_task.
    periodic = asyncio.create_task(index.scan_all())
    index.rescan_task = periodic
    assert await _wait_until(lambda: index.preindex), "проход не начался"

    response = await update_config(UserConfig(folders=[str(kept)]), cfg, index)

    assert response.status_code == 200
    with suppress(asyncio.CancelledError):
        await periodic
    await asyncio.wait({index.task})
    assert _rows(index.files_store, removed_prefix) == 0, "снятая папка снова в Lance"
    assert await _lsm_keys(index, removed_prefix) == [], "снятая папка снова в LSM"


@pytest.mark.asyncio
async def test_folder_change_keeps_periodic_rescan_alive(
    cfg: Config, index: Index, no_watcher, tmp_path, monkeypatch
):
    """stop_index() гасит и периодический проход, а start_reindex() поднимает его."""
    monkeypatch.setattr(cfg, "full_rescan_interval", 3600.0)

    async def nothing(emails_only: bool = False):
        return None

    index._scan_all = nothing  # type: ignore[method-assign]
    await index.start_periodic_rescan()
    folder = tmp_path / "docs"
    folder.mkdir()

    await update_config(UserConfig(folders=[str(folder)]), cfg, index)

    assert index.rescan_task is not None and not index.rescan_task.done(), (
        "после смены папок периодический проход не поднят"
    )


@pytest.mark.parametrize("stage", ["collect", "write"])
@pytest.mark.asyncio
async def test_email_disable_stops_email_stage_of_running_pass(
    email_cfg: Config, email_index: Index, no_watcher, monkeypatch, stage: str
):
    """Выключение почты обрывает почтовый этап идущего полного прохода.

    Иначе этап дописывает письма в пересозданную таблицу (они находятся при
    sources=["email"]), а позиции папок — в сброшенное состояние, и при
    повторном включении mbox пропускается как неизменённый. Сам проход при
    этом не отменяется.
    """
    reached = threading.Event()
    if stage == "collect":
        real_iter = email_scanner_module.iter_emails_as_metadata

        def slow_iter(*args, **kwargs):
            for item in real_iter(*args, **kwargs):
                reached.set()
                time.sleep(0.2)
                yield item

        monkeypatch.setattr(email_scanner_module, "iter_emails_as_metadata", slow_iter)
    else:
        real_chunks = parsers_module._process_email_chunks

        async def slow_chunks(*args, **kwargs):
            reached.set()
            await asyncio.sleep(0.3)
            async for chunk in real_chunks(*args, **kwargs):
                yield chunk

        monkeypatch.setattr(parsers_module, "_process_email_chunks", slow_chunks)

    email_index.task = asyncio.create_task(email_index.scan_all())
    assert await _wait_until(reached.is_set), "почтовый этап не начался"

    await update_config(
        UserConfig.model_validate({"email": {"enabled": False}}), email_cfg, email_index
    )
    await asyncio.wait({email_index.task})

    assert not email_index.task.cancelled(), "оборван весь проход, а не почтовый этап"
    assert email_index.email_store.count_matching("true") == 0, "письма дописаны"
    assert await _lsm_keys(email_index, "email_") == []
    assert email_index.emails_scanner.folder_state["INBOX"] == FolderState(), (
        "позиция папки записана в сброшенное состояние"
    )
    assert (await email_index.status()).is_indexing is False


# --- T058: выключение почты снимает отложенную пересборку её таблицы --------


@pytest.mark.asyncio
async def test_drop_email_index_clears_refresh_mark(
    email_index: Index, monkeypatch, caplog
):
    """Отложенная пересборка не падает раз в index_refresh_delay на удалённой таблице."""
    monkeypatch.setattr(email_index.cfg, "index_refresh_delay", 0.2)
    _ = email_index.email_store.table  # таблица есть, как во время прохода
    await email_index.ensure_index(email_index.email_store)

    with caplog.at_level(logging.ERROR):
        await email_index.drop_email_index()
        await asyncio.sleep(0.6)

    assert email_index.email_store.table_name not in email_index._dirty_stores
    failures = [r for r in caplog.records if "failed to refresh" in r.getMessage()]
    assert failures == [], "пересборка ходит в удалённую почтовую таблицу"


# --- T059: смена почтового корня закрывает прежний сканер сразу -------------


@pytest.mark.asyncio
async def test_root_change_does_not_resurrect_old_offsets(
    email_cfg: Config, tmp_path, monkeypatch
):
    """Прежний сканер не переписывает offsets.json на остановке демона.

    Иначе выключенная почта после рестарта и повторного включения стартует с
    позиций старого корня, и письма до них не индексируются.
    """
    other_root = make_profile(
        tmp_path / "tb2",
        {"INBOX": [make_plain_email("Другой ящик", "тело", msg_id="o@t")]},
    )
    index = Index(email_cfg)
    async with index:
        index.emails_scanner.folder_state["INBOX"] = FolderState(
            offset=2295, size=3068, mtime=1
        )
        monkeypatch.setattr(
            email_cfg,
            "email",
            EmailConfig(enabled=True, path_to_email_folder=str(other_root)),
        )
        await index._ensure_emails_scanner()
        # Выключение почты: offsets.json снят, позиции сброшены.
        await index.drop_email_index()

    state = json.loads((Path(email_cfg.lsm_root) / "offsets.json").read_text())
    assert state["folders"]["INBOX"]["offset"] == 0, (
        f"на остановке воскрешено состояние прежнего корня: {state}"
    )


@pytest.mark.asyncio
async def test_root_change_stops_email_stage_of_old_scanner(
    email_index: Index, email_cfg: Config, tmp_path, monkeypatch
):
    """Прежний сканер закрывается только после того, как его этап оборван."""
    started = asyncio.Event()

    async def nothing(*args, **kwargs):
        return None

    async def endless_email_pass(collect, scanner, store):
        if store is email_index.email_store:
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(email_index, "reconcile_deleted", nothing)
    monkeypatch.setattr(email_index, "_run_pass", endless_email_pass)
    email_index.task = asyncio.create_task(email_index.scan_all())
    await asyncio.wait_for(started.wait(), 5)
    other_root = make_profile(tmp_path / "tb2", {"INBOX": default_inbox_messages()})
    monkeypatch.setattr(
        email_cfg, "email", EmailConfig(enabled=True, path_to_email_folder=str(other_root))
    )

    scanner = await email_index._ensure_emails_scanner()

    assert scanner is not None and scanner.path == other_root
    await asyncio.wait_for(email_index.task, 5)


# --- T095: закрытие индекса гасит все фоновые задачи ------------------------


@pytest.mark.asyncio
async def test_closing_index_stops_email_polling_and_index_refresh(
    email_cfg: Config, monkeypatch
):
    """После закрытия индекса поллинг и пересборка не ходят в закрытые LSM и LanceDB."""
    monkeypatch.setattr(email_cfg, "index_refresh_delay", 30.0)
    index = Index(email_cfg)
    async with index:
        await index.start_emails_polling()
        await index.ensure_index()
        polling, refresh = index.emails_task, index._index_task
    try:
        assert polling.done(), "поллинг почты пережил закрытие индекса"
        assert refresh.done(), "отложенная пересборка пережила закрытие индекса"
    finally:
        for task in (polling, refresh):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_await_stopped_keeps_cancel_of_waiter():
    """Отмена того, кто ждёт остановки этапа, не теряется.

    Поллинг почты ждёт свёртки почтового этапа при смене корня; если в это
    время пришёл stop_emails_polling, поллинг обязан завершиться.
    """
    winding_down = asyncio.Event()

    async def stage():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            winding_down.set()
            await asyncio.sleep(0.2)
            raise

    inner = asyncio.create_task(stage())
    await asyncio.sleep(0)
    inner.cancel()

    async def polling():
        await _await_stopped(inner, "почтовый этап")
        return "продолжил"

    outer = asyncio.create_task(polling())
    await winding_down.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_await_stopped_swallows_cancel_of_stopped_task():
    """Отмена самой останавливаемой задачи — штатный исход, не ошибка."""
    inner = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    inner.cancel()
    await _await_stopped(inner, "этап")
    assert inner.cancelled()
